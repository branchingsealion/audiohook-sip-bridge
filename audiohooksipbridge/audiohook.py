import asyncio
import contextlib
import json
import logging
import os
import ssl
import time
import uuid
from dataclasses import dataclass, field

from websockets.asyncio.server import serve, ServerConnection
from websockets.exceptions import ConnectionClosed

from audiohooksipbridge.sip import SipClient
from audiohooksipbridge.sips import SipsClient
from audiohooksipbridge.uui import metadata_to_uui
from enum import StrEnum

LOGGER = logging.getLogger("audiohooksipbridge.audiohook")


class MessageType(StrEnum):
    open = "open"
    update = "update"
    pause = "pause"
    resume = "resume"
    ping = "ping"
    dtmf = "dtmf"
    sipInvite = "sipInvite"
    sipBye = "sipBye"
    close = "close"


@dataclass
class Session:
    ws: ServerConnection
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    opened_at: float = field(default_factory=time.time)
    paused: bool = False
    last_msg_at: float = field(default_factory=time.time)
    api_key: str | None = None
    queue: asyncio.Queue[bytes] = field(default_factory=lambda: asyncio.Queue(maxsize=100))
    meta: dict[str, str] = field(default_factory=dict)
    pump_task: asyncio.Task | None = None


class AudioHookServer:
    def __init__(self, host: str, port: int, api_key: str | None, sip_client: SipClient | SipsClient) -> None:
        self.host = host
        self.port = port
        self.api_key = api_key
        self._server = None
        self.sessions: dict[str, Session] = {}
        # Injected SIP client instance (UDP or TLS)
        self.sip: SipClient | SipsClient = sip_client

    @property
    def sessions_active(self) -> int:
        return len(self.sessions)

    async def _handle(self, ws: ServerConnection):
        session = Session(ws=ws)
        LOGGER.info(json.dumps({"event": "ws_connect", "session": session.id}))
        try:
            async for msg in ws:
                if isinstance(msg, (bytes, bytearray)):
                    await self._handle_binary(session, bytes(msg))
                else:
                    await self._handle_text(session, msg)
        except ConnectionClosed:
            pass
        finally:
            await self._close_session(session)

    async def _handle_text(self, session: Session, text: str):
        session.last_msg_at = time.time()
        try:
            data = json.loads(text)
        except Exception:
            await session.ws.close(code=1003, reason="invalid json")
            return
        typ_str = data.get("type")
        try:
            typ = MessageType(typ_str)
        except Exception:
            await session.ws.close(code=1003, reason="unsupported message")
            return
        if typ == MessageType.open:
            if self.api_key and data.get("apiKey") != self.api_key:
                await session.ws.close(code=1008, reason="unauthorized")
                return
            session.api_key = data.get("apiKey")
            session.meta = data.get("metadata", {})
            self.sessions[session.id] = session
            await session.ws.send(json.dumps({
                "type": "openAck",
                "sessionId": session.id,
                "metadata": session.meta,
            }))
            LOGGER.info(json.dumps({"event": "ws_open", "session": session.id, "metadata": session.meta}))
        elif typ == MessageType.update:
            # metadata update
            session.meta.update(data.get("metadata", {}))
            await session.ws.send(json.dumps({"type": "updateAck"}))
        elif typ == MessageType.pause:
            session.paused = True
            await session.ws.send(json.dumps({"type": "paused"}))
            # Map to SIP hold if a call is active
            try:
                self.sip.hold()
            except Exception:
                LOGGER.debug("SIP hold failed on pause", exc_info=True)
        elif typ == MessageType.resume:
            session.paused = False
            await session.ws.send(json.dumps({"type": "resumed"}))
            # Map to SIP resume (re-INVITE)
            try:
                self.sip.resume()
            except Exception:
                LOGGER.debug("SIP resume failed on resume", exc_info=True)
        elif typ == MessageType.ping:
            await session.ws.send(json.dumps({"type": "pong", "ts": time.time()}))
        elif typ == MessageType.dtmf:
            digits = (data.get("digits") or data.get("tones") or "").strip()
            if not digits:
                await session.ws.send(json.dumps({"type": "error", "error": "missing dtmf digits"}))
            else:
                try:
                    self.sip.send_dtmf(digits)
                    await session.ws.send(json.dumps({"type": "dtmfAck", "digits": digits}))
                except Exception:
                    LOGGER.exception("Failed to send DTMF")
                    await session.ws.send(json.dumps({"type": "error", "error": "dtmf failed"}))
        elif typ == MessageType.sipInvite:
            target = data.get("target") or data.get("uri")
            if not target:
                await session.ws.send(json.dumps({"type": "error", "error": "missing target uri"}))
                return
            await self._start_sip_for_session(session, target)
            await session.ws.send(json.dumps({"type": "sipInviteAck", "target": target}))
        elif typ == MessageType.sipBye:
            await self._stop_sip_for_session(session)
            await session.ws.send(json.dumps({"type": "sipByeAck"}))
        elif typ == MessageType.close:
            await session.ws.close(code=1000, reason="normal")
        else:
            await session.ws.close(code=1003, reason="unsupported message")

    @staticmethod
    async def _handle_binary(session: Session, payload: bytes):
        # Accept PCMU 8kHz frames; apply backpressure via queue
        if session.paused:
            return
        try:
            session.queue.put_nowait(payload)
        except asyncio.QueueFull:
            # backpressure: drop oldest
            _ = await session.queue.get()
            await session.queue.put(payload)

    async def _close_session(self, session: Session):
        # stop any pump task and SIP mapping
        await self._stop_sip_for_session(session)
        self.sessions.pop(session.id, None)
        LOGGER.info(json.dumps({"event": "ws_close", "session": session.id}))
        with contextlib.suppress(Exception):
            await session.ws.close()

    async def _pump_session_audio(self, session: Session):
        # Continuously read binary frames from session.queue and send to SIP
        while True:
            payload = await session.queue.get()
            if session.paused:
                continue
            if not payload:
                continue
            try:
                self.sip.send_pcmu_frame(payload)
            except Exception:
                LOGGER.debug("sip send_pcmu_frame failed", exc_info=True)

    async def _start_sip_for_session(self, session: Session, target: str):
        # route incoming RTP back to this session (bidirectional bridge)
        def _on_rtp_frame(payload: bytes) -> None:
            if session.paused:
                return
            try:
                asyncio.create_task(session.ws.send(payload))
            except Exception:
                LOGGER.debug("ws send(payload) failed", exc_info=True)
        # type: ignore[attr-defined]
        setattr(self.sip, "on_rtp_frame", _on_rtp_frame)
        # Prepare headers from AudioHook metadata (UUI)
        headers: dict[str, str] = {}
        try:
            uui = metadata_to_uui(session.meta)
            if uui:
                headers["User-to-User"] = uui
        except Exception:
            LOGGER.debug("Failed to render UUI from metadata", exc_info=True)
        # Start call (fire-and-forget basic)
        try:
            call = await self.sip.invite(target, headers=headers or None)
            session.meta["sip_call"] = str(True if call is not None else False)
        except Exception as e:
            LOGGER.exception("Failed to start SIP call: %s", e)
            await session.ws.send(json.dumps({"type": "error", "error": "sip invite failed"}))
            return
        # Start a pump task if not running
        if session.pump_task is None or session.pump_task.done():
            session.pump_task = asyncio.create_task(self._pump_session_audio(session))

    async def _stop_sip_for_session(self, session: Session):
        # Stop pump
        if session.pump_task:
            session.pump_task.cancel()
            with contextlib.suppress(Exception):
                await session.pump_task
            session.pump_task = None
        # Hang up an active SIP call if any
        try:
            self.sip.hangup()
        except Exception:
            LOGGER.debug("SIP hangup failed on session stop", exc_info=True)
        session.meta.pop("sip_call", None)

    async def start(self):
        # Enforce WSS with port 443 as per AudioHook specification
        certfile = os.getenv("WS_TLS_CERTFILE")
        keyfile = os.getenv("WS_TLS_KEYFILE")
        if not certfile or not keyfile:
            raise RuntimeError(
                "AudioHook requires WSS on port 443: provide WS_TLS_CERTFILE and WS_TLS_KEYFILE"
            )
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(certfile, keyfile)
        self._server = await serve(self._handle, self.host, 443, max_size=2 ** 20, ssl=ssl_ctx)
        LOGGER.info(json.dumps({"event": "ws_started", "host": self.host, "port": 443, "tls": True}))

    async def close(self):
        # stop accepting new connections and close
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        # drain sessions
        for s in list(self.sessions.values()):
            with contextlib.suppress(Exception):
                await self._stop_sip_for_session(s)
                await s.ws.close(code=1001, reason="server shutting down")
        self.sessions.clear()
        # stop SIP client
        with contextlib.suppress(Exception):
            await self.sip.stop()


async def start_audiohook_server(sip_client: SipClient | SipsClient) -> AudioHookServer:
    host = os.getenv("WS_HOST", "0.0.0.0")
    # Port is fixed to 443 per spec; WS_PORT env is ignored
    api_key = os.getenv("AUDIOHOOK_API_KEY")
    srv = AudioHookServer(host, 443, api_key, sip_client)
    await srv.start()
    return srv
