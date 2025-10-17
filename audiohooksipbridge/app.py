import asyncio
import json
import logging
import os
import signal
from contextlib import suppress

from aiohttp import web

from audiohooksipbridge.audiohook import start_audiohook_server, AudioHookServer
from audiohooksipbridge.sip import SipClient
from audiohooksipbridge.sips import SipsClient

LOGGER = logging.getLogger("audiohooksipbridge.app")


def json_logger_setup():
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        fmt='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)s}',
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(os.getenv("LOG_LEVEL", "INFO"))
    root.handlers.clear()
    root.addHandler(handler)


class BridgeApp:
    def __init__(self) -> None:
        self.stop_event = asyncio.Event()
        self.http_runner: web.AppRunner | None = None
        self.ws_server: AudioHookServer | None = None
        self.sip_client: SipClient | SipsClient | None = None

    async def healthz(self, request: web.Request) -> web.Response:
        return web.Response(text="ok", content_type="text/plain")

    async def metrics(self, request: web.Request) -> web.Response:
        # Minimal placeholder metrics
        lines = [
            "# HELP bridge_sessions_active Active AudioHook sessions",
            "# TYPE bridge_sessions_active gauge",
            f"bridge_sessions_active {self.ws_server.sessions_active if self.ws_server else 0}",
        ]
        return web.Response(text="\n".join(lines) + "\n", content_type="text/plain; version=0.0.4")

    async def _run_http(self) -> None:
        app = web.Application()
        app.add_routes([web.get("/healthz", self.healthz), web.get("/metrics", self.metrics)])
        self.http_runner = web.AppRunner(app)
        await self.http_runner.setup()
        site = web.TCPSite(self.http_runner, host=os.getenv("HTTP_HOST", "0.0.0.0"),
                           port=int(os.getenv("HTTP_PORT", "8080")))
        await site.start()
        LOGGER.info(json.dumps({"event": "http_started", "port": int(os.getenv("HTTP_PORT", "8080"))}))

    async def _stop_http(self) -> None:
        if self.http_runner:
            await self.http_runner.cleanup()

    async def run(self) -> None:
        # Start SIP/SIPS client first so it can be injected into the AudioHook server
        target = (
            os.getenv("SIP_TARGET")
            or os.getenv("SIP_URI")
            or os.getenv("TARGET_URI")
            or os.getenv("DESTINATION")
            or os.getenv("SIP_DESTINATION")
        )
        transport_env = (os.getenv("SIP_TRANSPORT") or "").lower()
        use_tls = False
        if target:
            scheme = target.strip().lower().split(":", 1)[0]
            use_tls = (scheme == "sips")
        elif transport_env in ("tls", "sips"):
            use_tls = True
        try:
            if use_tls:
                self.sip_client = SipsClient.from_env()
                scheme = "sips"
            else:
                self.sip_client = SipClient.from_env()
                scheme = "sip"
            await self.sip_client.start()
            LOGGER.info(json.dumps({"event": "sip_client_started", "scheme": scheme, "target": target}))
        except Exception:
            LOGGER.exception("Failed to start SIP client")
            raise

        # Now start AudioHook (WSS) with the initialized SIP client
        self.ws_server = await start_audiohook_server(self.sip_client)
        await self._run_http()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stop_event.set) # type: ignore[arg-type]

        await self.stop_event.wait()
        LOGGER.info(json.dumps({"event": "shutting_down"}))

        # Stop accepting, then drain
        if self.ws_server:
            await self.ws_server.close()
        await self._stop_http()


async def main():
    json_logger_setup()
    app = BridgeApp()
    await app.run()


if __name__ == "__main__":
    asyncio.run(main())
