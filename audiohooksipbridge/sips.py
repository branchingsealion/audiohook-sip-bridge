"""
Native SIPS (SIP over TLS) client using Python's ssl and asyncio.

Implements a minimal but functional SIP UA over TCP+TLS (default port 5061):
- INVITE with SDP offer (PCMU payload type 0)
- ACK/BYE and simple dialog state (Call-ID, tags, CSeq)
- RTP media over UDP (receive + send) after SDP answer

Notes:
- Digest authentication and REGISTER are not implemented. Many PBXes allow
  direct-dial without registration; if your environment requires auth, run the
  bridge behind a gateway that terminates TLS and handles auth.
- Hold/resume and DTMF are best-effort no-ops for now.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import socket
import ssl
import time
import contextlib
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from dataclasses import dataclass

# Local duplicates to avoid cross-module dependency on sip.py
@dataclass
class SipAccount:
    username: str
    password: str
    domain: str
    proxy: str | None = None
    port: int = 5061
    transport: str = "tls"
    tls_verify: bool = True


@dataclass
class SrtpConfig:
    enabled: bool = False
    profile: str = "AES_CM_128_HMAC_SHA1_80"
    local_key: str | None = None
    remote_key: str | None = None

LOGGER = logging.getLogger("audiohooksipbridge.sips")

CRLF = "\r\n"


def _gen_id(prefix: str = "") -> str:
    h = f"{prefix}{int(time.time()*1000):x}{random.getrandbits(40):010x}"
    return h


@dataclass
class _Dialog:
    call_id: str
    from_tag: str
    to_tag: str | None = None
    cseq: int = 1
    branch: str = ""
    local_addr: Tuple[str, int] | None = None
    remote_rtp: Tuple[str, int] | None = None


class SipsClient:
    def __init__(
        self,
        account: SipAccount,
        srtp: SrtpConfig | None = None,
        on_incoming_call: Callable[[object], None] | None = None,
        on_rtp_frame: Callable[[bytes], None] | None = None,
    ) -> None:
        self.account = account
        self.srtp = srtp or SrtpConfig(enabled=False)
        self.on_incoming_call = on_incoming_call
        self.on_rtp_frame = on_rtp_frame
        self._tls_reader: asyncio.StreamReader | None = None
        self._tls_writer: asyncio.StreamWriter | None = None
        self._rtp_sock: socket.socket | None = None
        self._rtp_task: asyncio.Task | None = None
        self._dialog: _Dialog | None = None
        self._ssrc = random.getrandbits(32)
        self._seq = 0
        self._ts = 0

    @staticmethod
    def from_env() -> "SipsClient":
        # mirror SipClient.from_env without recursion
        username = os.environ.get("SIP_USERNAME", "")
        password = os.environ.get("SIP_PASSWORD", "")
        domain = os.environ.get("SIP_DOMAIN", "")
        proxy = os.environ.get("SIP_PROXY") or None
        port = int(os.environ.get("SIP_PORT", "5061"))
        tls_verify = os.environ.get("SIP_TLS_VERIFY", "true").lower() in ("1", "true", "yes")
        acc = SipAccount(username=username, password=password, domain=domain, proxy=proxy, port=port, transport="tls", tls_verify=tls_verify)
        srtp = SrtpConfig(
            enabled=os.environ.get("SRTP", "false").lower() in ("1", "true", "yes"),
            profile=os.environ.get("SRTP_PROFILE", "AES_CM_128_HMAC_SHA1_80"),
            local_key=os.environ.get("SRTP_LOCAL_KEY"),
            remote_key=os.environ.get("SRTP_REMOTE_KEY"),
        )
        return SipsClient(acc, srtp)

    async def start(self) -> None:
        # Establish a TLS connection used for SIP signaling
        host = (self.account.proxy or self.account.domain).split(":")[0]
        port = self.account.port or 5061
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        cafile = os.getenv("SIP_TLS_CAFILE")
        if cafile:
            try:
                ctx.load_verify_locations(cafile)
            except Exception:
                LOGGER.exception("Failed to load SIP TLS CA file: %s", cafile)
        if not self.account.tls_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self._tls_reader, self._tls_writer = await asyncio.open_connection(host=host, port=port, ssl=ctx, server_hostname=host if self.account.tls_verify else None)
        LOGGER.info("SIPS connected (server=%s:%s, verify=%s)", host, port, self.account.tls_verify)

    async def stop(self) -> None:
        # stop RTP task
        if self._rtp_task:
            self._rtp_task.cancel()
            with contextlib.suppress(Exception):
                await self._rtp_task
            self._rtp_task = None
        # close RTP socket
        if self._rtp_sock:
            with contextlib.suppress(Exception):
                self._rtp_sock.close()
            self._rtp_sock = None
        # close TLS
        if self._tls_writer:
            with contextlib.suppress(Exception):
                self._tls_writer.close()
            self._tls_writer = None
            self._tls_reader = None
        self._dialog = None

    async def invite(self, target_uri: str, headers: dict[str, str] | None = None) -> object:
        if not self._tls_writer or not self._tls_reader:
            await self.start()
        # prepare dialog
        call_id = _gen_id("cid-")
        from_tag = _gen_id("ft-")
        branch = _gen_id("z9hG4bK-")
        self._dialog = _Dialog(call_id=call_id, from_tag=from_tag, branch=branch)
        # open RTP socket early to put into SDP
        self._rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._rtp_sock.bind((os.getenv("RTP_BIND_IP", "0.0.0.0"), 0))
        local_ip, local_port = self._rtp_sock.getsockname()
        self._dialog.local_addr = (local_ip, local_port)
        # build SDP offer
        sdp = self._build_sdp(local_ip, local_port)
        # normalize target
        target = target_uri.strip()
        if not target.startswith("sip:") and not target.startswith("sips:"):
            target = f"sips:{target}"
        # host for Contact
        contact_host = os.getenv("SIP_CONTACT_HOST", local_ip)
        # construct INVITE
        req = self._render_request(
            method="INVITE",
            request_uri=target,
            headers={
                "Via": f"SIP/2.0/TLS {contact_host};branch={branch}",
                "Max-Forwards": "70",
                "From": f"\"{self.account.username}\" <sip:{self.account.username}@{self.account.domain}>;tag={from_tag}",
                "To": f"<{target}>",
                "Call-ID": call_id,
                "CSeq": "1 INVITE",
                "Contact": f"<sips:{self.account.username}@{contact_host}>",
                "Content-Type": "application/sdp",
                **(headers or {}),
            },
            body=sdp,
        )
        await self._send(req)
        # read responses until 200 OK
        status, resp_headers, body = await self._read_response()
        # skip provisional
        while status and status < 200:
            status, resp_headers, body = await self._read_response()
        if not status or status >= 300:
            raise RuntimeError(f"INVITE failed with status {status}")
        # parse To tag and SDP answer
        to_hdr = resp_headers.get("to", "")
        to_tag = _parse_param(to_hdr, "tag") if to_hdr else None
        if self._dialog:
            self._dialog.to_tag = to_tag
        if body:
            r_ip, r_port = _parse_sdp_connection(body)
            self._dialog.remote_rtp = (r_ip, r_port)
        # send ACK
        ack = self._render_request(
            method="ACK",
            request_uri=target,
            headers={
                "Via": f"SIP/2.0/TLS {contact_host};branch={branch}",
                "Max-Forwards": "70",
                "From": f"\"{self.account.username}\" <sip:{self.account.username}@{self.account.domain}>;tag={from_tag}",
                "To": f"<{target}>;tag={to_tag}" if to_tag else f"<{target}>",
                "Call-ID": call_id,
                "CSeq": "1 ACK",
                "Contact": f"<sips:{self.account.username}@{contact_host}>",
            },
        )
        await self._send(ack)
        # start RTP receive task
        self._start_rtp_receiver()
        return {"callId": call_id, "toTag": to_tag}

    def send_pcmu_frame(self, payload: bytes) -> None:
        if not payload or not self._dialog or not self._dialog.remote_rtp or not self._rtp_sock:
            return
        self._seq = (self._seq + 1) & 0xFFFF
        self._ts = (self._ts + len(payload)) & 0xFFFFFFFF
        header = bytearray(12)
        # V=2,P=0,X=0,CC=0
        header[0] = 0x80
        # M=0, PT=0 (PCMU)
        header[1] = 0x00
        header[2] = (self._seq >> 8) & 0xFF
        header[3] = self._seq & 0xFF
        header[4] = (self._ts >> 24) & 0xFF
        header[5] = (self._ts >> 16) & 0xFF
        header[6] = (self._ts >> 8) & 0xFF
        header[7] = self._ts & 0xFF
        header[8] = (self._ssrc >> 24) & 0xFF
        header[9] = (self._ssrc >> 16) & 0xFF
        header[10] = (self._ssrc >> 8) & 0xFF
        header[11] = self._ssrc & 0xFF
        pkt = bytes(header) + payload
        try:
            self._rtp_sock.sendto(pkt, self._dialog.remote_rtp)
        except Exception:
            LOGGER.debug("RTP send failed", exc_info=True)

    def hangup(self) -> None:
        if not self._dialog or not self._tls_writer:
            return
        self._dialog.cseq += 1
        target = f"sips:{self.account.username}@{self.account.domain}"
        contact_host = (self._dialog.local_addr[0] if self._dialog.local_addr else "localhost")
        req = self._render_request(
            method="BYE",
            request_uri=target,
            headers={
                "Via": f"SIP/2.0/TLS {contact_host};branch={self._dialog.branch}",
                "Max-Forwards": "70",
                "From": f"\"{self.account.username}\" <sip:{self.account.username}@{self.account.domain}>;tag={self._dialog.from_tag}",
                "To": f"<sips:{self.account.username}@{self.account.domain}>;tag={self._dialog.to_tag}" if self._dialog.to_tag else f"<sips:{self.account.username}@{self.account.domain}>",
                "Call-ID": self._dialog.call_id,
                "CSeq": f"{self._dialog.cseq} BYE",
                "Contact": f"<sips:{self.account.username}@{contact_host}>",
            },
        )
        asyncio.create_task(self._send(req))

    def send_dtmf(self, digits: str) -> None:
        LOGGER.warning("RTP DTMF not implemented for SIPS yet; ignoring: %s", digits)

    def hold(self) -> None:
        LOGGER.warning("SIPS hold not implemented; ignoring")

    def resume(self) -> None:
        LOGGER.warning("SIPS resume not implemented; ignoring")

    # Internals
    def _build_sdp(self, ip: str, port: int) -> str:
        lines = [
            "v=0",
            f"o=- 0 0 IN IP4 {ip}",
            f"s=audiohook-audiohooksipbridge",
            f"c=IN IP4 {ip}",
            "t=0 0",
            f"m=audio {port} RTP/AVP 0",
            "a=rtpmap:0 PCMU/8000",
        ]
        return CRLF.join(lines) + CRLF

    def _render_request(self, method: str, request_uri: str, headers: dict[str, str], body: str | None = None) -> str:
        hlines = [f"{method} {request_uri} SIP/2.0"]
        for k, v in headers.items():
            hlines.append(f"{k}: {v}")
        if body is not None:
            hlines.append(f"Content-Length: {len(body.encode('utf-8'))}")
        else:
            hlines.append("Content-Length: 0")
        hlines.append("")
        h = CRLF.join(hlines)
        return h + (body or "")

    async def _send(self, data: str) -> None:
        assert self._tls_writer is not None
        self._tls_writer.write(data.encode("utf-8"))
        await self._tls_writer.drain()

    async def _read_response(self) -> tuple[Optional[int], dict[str, str], str | None]:
        assert self._tls_reader is not None
        headers: dict[str, str] = {}
        status: Optional[int] = None
        body = None
        # read until header/body separator
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = await self._tls_reader.read(4096)
            if not chunk:
                break
            buf += chunk
        parts = buf.split(b"\r\n\r\n", 1)
        header_bytes = parts[0]
        rest = parts[1] if len(parts) > 1 else b""
        header_text = header_bytes.decode("utf-8", errors="ignore")
        lines = header_text.split("\r\n")
        if lines and lines[0].startswith("SIP/2.0 "):
            try:
                status = int(lines[0].split()[1])
            except Exception:
                status = None
        for ln in lines[1:]:
            if not ln:
                continue
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        # content length
        clen = int(headers.get("content-length", "0"))
        if clen > 0:
            # rest may already include some of the body
            while len(rest) < clen:
                chunk = await self._tls_reader.read(clen - len(rest))
                if not chunk:
                    break
                rest += chunk
            body = rest[:clen].decode("utf-8", errors="ignore")
        return status, headers, body

    def _start_rtp_receiver(self) -> None:
        if not self._rtp_sock:
            return
        loop = asyncio.get_event_loop()
        self._rtp_sock.setblocking(False)

        async def _recv():
            while True:
                try:
                    data, _addr = await loop.run_in_executor(None, self._rtp_sock.recvfrom, 2048)
                    if len(data) < 12:
                        continue
                    pt = data[1] & 0x7F
                    if pt != 0:  # PCMU only
                        continue
                    payload = data[12:]
                    if self.on_rtp_frame:
                        try:
                            self.on_rtp_frame(payload)
                        except Exception:
                            LOGGER.debug("on_rtp_frame callback failed", exc_info=True)
                except asyncio.CancelledError:
                    break
                except Exception:
                    await asyncio.sleep(0.01)
                    continue
        self._rtp_task = asyncio.create_task(_recv())


def _parse_param(value: str, key: str) -> Optional[str]:
    # parse key=value from header parameter list
    for part in value.split(";"):
        part = part.strip()
        if part.startswith(key + "="):
            return part.split("=", 1)[1]
    return None


def _parse_sdp_connection(sdp: str) -> Tuple[str, int]:
    ip = "127.0.0.1"
    port = 4000
    for line in sdp.splitlines():
        line = line.strip()
        if line.startswith("c=IN IP4 "):
            ip = line.split()[2]
        elif line.startswith("m=audio "):
            try:
                port = int(line.split()[1])
            except Exception:
                pass
    return ip, port


__all__ = [
    "SipsClient",
]
