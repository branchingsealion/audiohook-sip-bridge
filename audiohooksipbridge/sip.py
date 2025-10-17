"""
SIP client integration using pyVoIP with support for UDP signaling.
A separate module sips.py provides SIPS (TLS) signaling via Python's ssl module.

Media is constrained to G.711 μ-law (PCMU) at the signaling layer; actual media
bridging is optional and implementation-specific.

This module focuses on signaling and call control, avoiding direct use of system
sound devices. Callers can feed/consume PCMU frames explicitly.
"""
from __future__ import annotations

import asyncio
import logging
import os
import contextlib
from dataclasses import dataclass
from typing import Callable

LOGGER = logging.getLogger("audiohooksipbridge.sip")


@dataclass
class SipAccount:
    username: str
    password: str
    domain: str
    proxy: str | None = None
    port: int = 5060
    transport: str = "auto"  # auto|tls|tcp|udp (preference: tls -> tcp -> udp)
    tls_verify: bool = True


@dataclass
class SrtpConfig:
    # Placeholder for future SRTP integration via PJSIP built-ins.
    enabled: bool = False
    profile: str = "AES_CM_128_HMAC_SHA1_80"
    local_key: str | None = None
    remote_key: str | None = None


class SipClient:
    """Minimal SIP UA based on pyVoIP (UDP signaling).

    Exposes hooks to:
    - on_incoming_call: invoked with call object
    - on_rtp_frame: invoked with each incoming PCMU RTP payload (20 ms preferred)
    """

    def __init__(self,
                 account: SipAccount,
                 srtp: SrtpConfig | None = None,
                 on_incoming_call: Callable[[object], None] | None = None,
                 on_rtp_frame: Callable[[bytes], None] | None = None,
                 ) -> None:
        self._phone = None
        self.account = account
        self.srtp = srtp or SrtpConfig(enabled=False)
        self.on_incoming_call = on_incoming_call
        self.on_rtp_frame = on_rtp_frame
        # pjsua2 objects
        self._ep = None  # Endpoint
        self._acc = None  # Account
        self._current_call = None  # Call
        self._rtp_receiver_task: asyncio.Task | None = None

    @staticmethod
    def from_env() -> "SipClient":
        """Build a UDP-based SIP client from environment variables.
        Note: TLS (SIPS) selection is handled by app.py; this factory only returns a UDP client.
        """
        acc = SipAccount(
            username=os.environ.get("SIP_USERNAME", ""),
            password=os.environ.get("SIP_PASSWORD", ""),
            domain=os.environ.get("SIP_DOMAIN", ""),
            proxy=os.environ.get("SIP_PROXY") or None,
            port=int(os.environ.get("SIP_PORT", "5060")),
            transport="udp",
            tls_verify=os.environ.get("SIP_TLS_VERIFY", "true").lower() in ("1", "true", "yes"),
        )
        srtp = SrtpConfig(
            enabled=os.environ.get("SRTP", "false").lower() in ("1", "true", "yes"),
            profile=os.environ.get("SRTP_PROFILE", "AES_CM_128_HMAC_SHA1_80"),
            local_key=os.environ.get("SRTP_LOCAL_KEY"),
            remote_key=os.environ.get("SRTP_REMOTE_KEY"),
        )
        return SipClient(acc, srtp)

    async def start(self) -> None:
        """Start pyVoIP SIP client (UDP). For TLS, see SipsClient."""
        # PyVoIP is synchronous internally; we wrap it with asyncio-compatible API.
        try:
            from pyVoIP.VoIP import VoIPPhone  # type: ignore
        except Exception as e:  # pragma: no cover - runtime dependency
            raise RuntimeError(
                "pyVoIP is required at runtime. Please install pyVoIP."
            ) from e

        transport = (self.account.transport or "udp").lower()
        if transport != "udp":
            if transport == "tls":
                raise RuntimeError("SipClient.start called with transport=tls; use SipsClient for TLS.")
            if transport == "tcp":
                raise NotImplementedError("SIP TCP transport is not supported. Do not fallback to UDP implicitly.")
            # Any other value is invalid for this client
            raise ValueError(f"Unsupported SIP transport for SipClient: {self.account.transport}")

        server = self.account.proxy or self.account.domain
        bind_ip = os.getenv("SIP_BIND_IP", "0.0.0.0")
        sip_bind_port = int(os.getenv("SIP_BIND_PORT", str(self.account.port or 5060)))

        # Initialize phone and start (register + receive loop)
        self._phone = VoIPPhone(
            server=server,
            port=self.account.port,
            username=self.account.username,
            password=self.account.password,
            myIP=bind_ip,
            callCallback=self._on_pyvoip_event,
            sipPort=sip_bind_port,
        )
        self._phone.start()
        LOGGER.info("pyVoIP SIP client started (UDP, server=%s, port=%s)", server, self.account.port)

    async def stop(self) -> None:
        # Stop RTP reader
        try:
            if self._rtp_receiver_task is not None:
                self._rtp_receiver_task.cancel()
                with contextlib.suppress(Exception):
                    await self._rtp_receiver_task
                self._rtp_receiver_task = None
        except Exception:
            LOGGER.debug("Error stopping RTP reader", exc_info=True)
        # Hangup active call
        try:
            if self._current_call is not None:
                with contextlib.suppress(Exception):
                    self._current_call.hangup()
                self._current_call = None
        except Exception:
            LOGGER.debug("Error hanging up call", exc_info=True)
        # Stop phone
        try:
            if getattr(self, "_phone", None) is not None:
                with contextlib.suppress(Exception):
                    self._phone.stop()
                self._phone = None
        except Exception:
            LOGGER.debug("Error stopping pyVoIP phone", exc_info=True)

    # Outbound call helper (basic):
    async def invite(self, target_uri: str, headers: dict[str, str] | None = None) -> object:
        if getattr(self, "_phone", None) is None:
            raise RuntimeError("SIP client not started")

        # Normalize/parse target
        raw = target_uri.strip()
        if raw.startswith("sip:"):
            raw = raw[4:]
        elif raw.startswith("sips:"):
            raw = raw[5:]
        number = raw.split("@")[0]

        # Place call using pyVoIP
        call = self._phone.call(number)
        self._current_call = call
        # Start RTP reader to feed on_rtp_frame if provided
        self._start_rtp_reader()
        if headers:
            LOGGER.debug("Custom headers requested on INVITE, but pyVoIP does not support arbitrary INVITE headers; ignoring: %s", list(headers.keys()))
        return call

    # RTP send (PCMU): expects payload already μ-law encoded. 20ms = 160 bytes @8kHz 8-bit.
    def send_pcmu_frame(self, payload: bytes) -> None:
        if not payload:
            return
        try:
            if self._current_call is not None:
                # pyVoIP will packetize and send on all RTP clients
                self._current_call.write_audio(payload)
        except Exception:
            LOGGER.debug("send_pcmu_frame failed", exc_info=True)

    # Call control primitives compatible with AudioHook
    def hangup(self) -> None:
        try:
            if self._current_call is not None:
                self._current_call.hangup()
                self._current_call = None
        except Exception:
            LOGGER.debug("hangup failed", exc_info=True)

    def send_dtmf(self, digits: str) -> None:
        """Best-effort DTMF using pyVoIP RTP EVENT if supported by RTP clients.

        Note: pyVoIP does not expose a stable public API for sending RFC2833
        events from VoIPCall. We attempt common method names on RTP clients,
        otherwise this is a no-op with a debug log.
        """
        if not digits:
            return
        try:
            call = getattr(self, "_current_call", None)
            if call is None:
                return
            clients = getattr(call, "RTPClients", []) or []
            for d in digits:
                for c in list(clients):
                    try:
                        if hasattr(c, "send_dtmf"):
                            c.send_dtmf(d)
                        elif hasattr(c, "sendDTMF"):
                            c.sendDTMF(d)
                        elif hasattr(c, "dtmf"):
                            # Some implementations expose a dtmf method
                            c.dtmf(d)
                        else:
                            LOGGER.debug("RTPClient has no DTMF send API; skipping")
                    except Exception:
                        LOGGER.debug("RTPClient DTMF send failed", exc_info=True)
        except Exception:
            LOGGER.debug("send_dtmf failed", exc_info=True)

    def hold(self) -> None:
        """Best-effort hold: no-op for pyVoIP baseline; log for visibility."""
        try:
            if self._current_call is not None:
                LOGGER.debug("Hold requested, but pyVoIP has no standard hold API; ignoring")
        except Exception:
            LOGGER.debug("hold failed", exc_info=True)

    def resume(self) -> None:
        """Best-effort resume: no-op for pyVoIP baseline; log for visibility."""
        try:
            if self._current_call is not None:
                LOGGER.debug("Resume requested, but pyVoIP has no standard resume API; ignoring")
        except Exception:
            LOGGER.debug("resume failed", exc_info=True)


    # Internal: callback from pyVoIP for incoming calls and some SIP events
    def _on_pyvoip_event(self, obj: object) -> None:
        try:
            # Incoming INVITE will deliver a VoIPCall instance
            # We detect by presence of 'answer' attribute
            if hasattr(obj, "answer"):
                call = obj  # likely pyVoIP.VoIP.VoIPCall
                self._current_call = call
                if self.on_incoming_call:
                    try:
                        self.on_incoming_call(call)
                    except Exception:
                        LOGGER.debug("on_incoming_call user hook failed", exc_info=True)
                else:
                    # Auto-answer
                    try:
                        call.answer()
                    except Exception:
                        LOGGER.debug("Auto-answer failed", exc_info=True)
                # Start RTP reader if needed
                self._start_rtp_reader()
            else:
                # Unrecognized callback object; ignore
                LOGGER.debug("_on_pyvoip_event received non-call object: %r", obj)
        except Exception:
            LOGGER.debug("_on_pyvoip_event error", exc_info=True)

    def _start_rtp_reader(self) -> None:
        if self.on_rtp_frame is None:
            return
        if self._rtp_receiver_task is not None and not self._rtp_receiver_task.done():
            return

        async def _reader() -> None:
            try:
                while getattr(self, "_current_call", None) is not None:
                    try:
                        # Block in thread to avoid blocking event loop
                        data: bytes = await asyncio.to_thread(self._current_call.read_audio, 160, True)
                        if data and self.on_rtp_frame:
                            self.on_rtp_frame(data)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # Drop frame on error, continue
                        LOGGER.debug("RTP read failed", exc_info=True)
                        await asyncio.sleep(0.005)
            except asyncio.CancelledError:
                pass
            except Exception:
                LOGGER.debug("RTP reader crashed", exc_info=True)

        self._rtp_receiver_task = asyncio.create_task(_reader())

# Convenience: wire SIP ↔ WebSocket audiohooksipbridge minimal path
class SipWsBridge:
    """Bridge RTP PCMU frames from SIP into an AudioHookServer session queue,
    and optionally send frames from the session to the SIP call.
    """

    def __init__(self, sip: SipClient, session_queue: "asyncio.Queue[bytes]") -> None:
        self.sip = sip
        self.queue = session_queue

    def on_rtp_frame(self, payload: bytes) -> None:
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            _ = self.queue.get_nowait()
            self.queue.put_nowait(payload)


__all__ = [
    "SipAccount",
    "SrtpConfig",
    "SipClient",
    "SipWsBridge",
]
