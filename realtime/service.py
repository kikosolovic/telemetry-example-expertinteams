"""Wires the components of `realtime` together and runs them on one event loop."""

from __future__ import annotations

import asyncio
import logging
import ssl

from aiohttp import web

from . import config
from .gateway import Gateway
from .listen import listen_forever
from .registry import Registry
from .rtc import RtcServer
from .udp import UdpListener
from .writer import TelemetryWriter, conninfo_from_django

log = logging.getLogger("realtime")


class Service:
    def __init__(self):
        self.registry = Registry()
        self.writer = TelemetryWriter()
        self.udp = UdpListener(self.registry)
        self.gateway = Gateway(self)
        self.rtc = RtcServer(self)
        self._finalisers: dict[int, asyncio.Task] = {}
        self._loaded = asyncio.Event()

    # ------------------------------------------------------------ events

    async def on_db_connected(self) -> None:
        pending = await self.registry.load_all()
        for s in pending:
            self._schedule_finalise(s.pk)
        # After a reload, tell every connected board its current state.
        for dev in self.registry.devices.values():
            self.gateway.push_state(dev.id)
        self._loaded.set()

    async def on_event(self, ev: dict) -> None:
        """Handle a NOTIFY from Django. Payloads carry ids only; we re-read the row."""
        kind = ev.get("event")
        if kind == "session_started":
            await self.registry.refresh_session(ev["session"])
            self.gateway.push_state(ev["device"])            # -> board: STATE stream_on
        elif kind == "session_ended":
            s = await self.registry.refresh_session(ev["session"])
            self.gateway.push_state(ev["device"])            # -> board: STATE stream_off
            if s:
                for v in list(s.viewers):
                    v.send_event({"type": "session_ended", "session": s.id})
            # Keep the session around for late resends, then write its gaps.
            self._schedule_finalise(ev["session"])
        elif kind == "device_changed":
            await self.registry.refresh_device(ev["device"])
        else:
            log.warning("unknown event %r", ev)

    def _schedule_finalise(self, session_id: int) -> None:
        if session_id in self._finalisers:
            return
        s = self.registry.sessions.get(session_id)
        delay = 0.0
        if s and s.ended_us:
            from .registry import now_us
            delay = max(0.0, config.SESSION_GRACE_S - (now_us() - s.ended_us) / 1e6)

        async def finalise():
            await asyncio.sleep(delay)
            try:
                await self.writer.finalise_session(session_id)
            finally:
                gone = self.registry.sessions.pop(session_id, None)
                self._finalisers.pop(session_id, None)
                # Close any viewers still watching the finished session.
                if gone:
                    for v in list(gone.viewers):
                        await v.pc.close()

        log.info("session %s: finalising in %.0f s (grace for late resends)", session_id, delay)
        self._finalisers[session_id] = asyncio.create_task(finalise())

    def broadcast_board_status(self, device_id: int, online: bool) -> None:
        for s in self.registry.sessions.values():
            if s.device_id == device_id:
                for v in list(s.viewers):
                    v.send_event({"type": "board", "device": device_id, "online": online})

    # ------------------------------------------------------------ periodic

    async def latency_reporter(self) -> None:
        """Once a second: send each viewer its latency numbers on `events`,
        and every 5 s log p50/p95 per session (SCRUM-180)."""
        tick = 0
        while True:
            await asyncio.sleep(1)
            tick += 1
            for s in list(self.registry.sessions.values()):
                dev = self.registry.devices.get(s.device_id)
                p50, p95 = s.latency.percentile(0.5), s.latency.percentile(0.95)
                offset = dev.clock.offset_us if dev else None
                for v in list(s.viewers):
                    total = (p50 + v.server_client_ms) if (p50 is not None and v.server_client_ms is not None) else None
                    v.send_event({
                        "type": "latency",
                        "board_server_p50_ms": p50 or 0.0, "board_server_p95_ms": p95 or 0.0,
                        "server_client_ms": v.server_client_ms, "total_ms": total,
                        "clock_offset_ms": None if offset is None else offset / 1000,
                    })
                if tick % 5 == 0 and s.running and p50 is not None:
                    sc = [v.server_client_ms for v in s.viewers if v.server_client_ms is not None]
                    log.info("session %s latency: board->server p50 %.1f ms p95 %.1f ms | server->client %s | "
                             "clock offset %s ms | udp ok %d dropped %d | viewers %d",
                             s.id, p50, p95, ", ".join(f"{x:.1f} ms" for x in sc) or "-",
                             f"{offset / 1000:.1f}" if offset is not None else "?",
                             dev.udp_ok if dev else 0, dev.udp_dropped if dev else 0, len(s.viewers))

    # ------------------------------------------------------------ run

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        await self.writer.open()

        listener = asyncio.create_task(listen_forever(conninfo_from_django(), self.on_db_connected, self.on_event))
        await self._loaded.wait()        # don't accept packets before we know the devices

        # UDP: live packets
        udp_transport, _ = await loop.create_datagram_endpoint(lambda: self.udp, local_addr=(config.BIND, config.UDP_PORT))

        # TCP: board gateway (TLS when a cert is configured)
        tls = None
        if config.TLS_CERT and config.TLS_KEY:
            tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            tls.load_cert_chain(config.TLS_CERT, config.TLS_KEY)
        tcp_server = await asyncio.start_server(self.gateway.handle, config.BIND, config.TCP_PORT, ssl=tls)

        # HTTP: POST /rtc/offer
        runner = web.AppRunner(self.rtc.app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, config.BIND, config.HTTP_PORT).start()

        log.info("realtime up: udp/%d (live), tcp/%d (gateway%s), http/%d (/rtc/offer)",
                 config.UDP_PORT, config.TCP_PORT, ", TLS" if tls else ", plain TCP - demo only",
                 config.HTTP_PORT)
        reporter = asyncio.create_task(self.latency_reporter())
        try:
            await asyncio.Event().wait()   # run until cancelled (Ctrl+C)
        finally:
            reporter.cancel()
            listener.cancel()
            udp_transport.close()
            tcp_server.close()
            await runner.cleanup()
            await self.writer.close()
