"""WebRTC side: aiortc peers + HTTP POST signalling (SCRUM-64/78/79/80).

Signalling is one HTTP request: the browser POSTs a *complete* offer (it waited
for ICE gathering) and gets back a *complete* answer (aiortc always gathers
all its candidates inside setLocalDescription). Nothing needs to be trickled
afterwards and every server->client push goes over data channels -- so there
is no WebSocket and no Django Channels/Redis.

All peers live in this one event loop, next to the UDP listener, which is what
lets `udp.py` push a sample to a viewer with a plain method call.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import TYPE_CHECKING

from aiohttp import web
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from django.core import signing

from boards.views import RTC_TOKEN_MAX_AGE, RTC_TOKEN_SALT

from . import config

if TYPE_CHECKING:
    from .registry import SessionState
    from .service import Service

log = logging.getLogger("realtime.rtc")

# If the browser can't drain the unreliable channel fast enough, aiortc would
# buffer messages without limit. For live data, an old sample is worthless:
# drop instead of queueing (newer data is already behind it).
MAX_BUFFERED_BYTES = 64 * 1024


class Viewer:
    """One browser/app watching one session."""

    def __init__(self, pc: RTCPeerConnection, user_id: int, session: "SessionState"):
        self.id = uuid.uuid4().hex[:8]
        self.pc, self.user_id, self.session = pc, user_id, session
        self.events = None            # reliable, ordered channel
        self.stability = None         # ordered:false, maxRetransmits:0
        # When each (boot, seq) was sent -- to measure server->client latency
        # from the client's once-a-second echo. Bounded: ~5 s of history.
        self.sent_at: dict[tuple[int, int], float] = {}
        self.server_client_ms: float | None = None
        self.dropped_backpressure = 0

    # Called from the UDP hot path. Must stay synchronous and cheap.
    def send_stability(self, msg: str, boot: int, seq: int) -> None:
        ch = self.stability
        if ch is None or ch.readyState != "open":
            return
        if ch.bufferedAmount > MAX_BUFFERED_BYTES:
            self.dropped_backpressure += 1
            return
        ch.send(msg)
        self.sent_at[(boot, seq)] = time.perf_counter()
        if len(self.sent_at) > 512:
            self.sent_at.pop(next(iter(self.sent_at)))   # dicts keep insertion order: drop oldest

    def send_event(self, payload: dict) -> None:
        if self.events is not None and self.events.readyState == "open":
            self.events.send(json.dumps(payload, separators=(",", ":")))

    def on_event_message(self, raw) -> None:
        try:
            m = json.loads(raw)
        except (TypeError, ValueError):
            return
        if m.get("type") == "echo":
            # The client tells us which seq it just *drew*. Round trip =
            # server->client + wait for animation frame + client->server.
            # Half of it approximates the one-way server -> screen time.
            # pop(): each send time is used once, so a repeated echo of a stale
            # seq (stream stalled) can't produce a bogus, ever-growing number.
            sent = self.sent_at.pop((m.get("boot"), m.get("seq")), None)
            if sent is not None:
                one_way = (time.perf_counter() - sent) * 1000 / 2
                # Light smoothing so the number on screen doesn't flicker.
                prev = self.server_client_ms
                self.server_client_ms = one_way if prev is None else 0.7 * prev + 0.3 * one_way


class RtcServer:
    def __init__(self, service: "Service"):
        self.service = service
        self.peers: set[RTCPeerConnection] = set()
        ice = [RTCIceServer(urls=u) for u in config.ICE_SERVERS]
        self.rtc_config = RTCConfiguration(iceServers=ice)

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._cors])
        app.router.add_post("/rtc/offer", self.offer)
        app.router.add_route("OPTIONS", "/rtc/offer", self.preflight)
        app.router.add_get("/healthz", self.health)
        app.on_shutdown.append(self._close_all)
        return app

    # ---- CORS (demo only; behind a same-origin reverse proxy this goes away)

    @web.middleware
    async def _cors(self, request, handler):
        resp = await handler(request)
        origin = request.headers.get("Origin")
        if origin in config.ALLOWED_ORIGINS:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
            resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
            resp.headers["Vary"] = "Origin"
        return resp

    async def preflight(self, request):
        return web.Response(status=204)

    async def health(self, request):
        reg = self.service.registry
        return web.json_response({
            "peers": len(self.peers),
            "devices_online": [d.id for d in reg.devices.values() if d.conn],
            "sessions": {s.id: {"running": s.running, "viewers": len(s.viewers)} for s in reg.sessions.values()},
            "udp_dropped": self.service.udp.dropped,
        })

    # ---- signalling ------------------------------------------------------

    async def offer(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            # The token was signed by Django *after* it checked that this user
            # may view this session (views.rtc_token). Verifying it here needs
            # only the shared SECRET_KEY -- no DB call. max_age bounds replay.
            claims = signing.loads(body["token"], salt=RTC_TOKEN_SALT, max_age=RTC_TOKEN_MAX_AGE)
            offer = RTCSessionDescription(sdp=body["sdp"], type=body["type"])
        except signing.BadSignature:      # includes SignatureExpired
            return web.Response(status=403, text="invalid or expired token")
        except (KeyError, ValueError, TypeError):
            return web.Response(status=400, text="bad offer")

        session = self.service.registry.sessions.get(claims["sid"])
        if session is None:
            return web.Response(status=404, text="session not active")

        pc = RTCPeerConnection(self.rtc_config)
        self.peers.add(pc)
        viewer = Viewer(pc, claims["uid"], session)

        @pc.on("datachannel")
        def on_datachannel(channel):
            # The *client* created both channels before its offer (so their
            # options, e.g. maxRetransmits=0, came from the browser and apply
            # in both directions). They show up here once open.
            if channel.label == "events":
                viewer.events = channel
                channel.on("message", viewer.on_event_message)
                dev = self.service.registry.devices.get(session.device_id)
                viewer.send_event({"type": "hello", "session": session.id, "running": session.running,
                                   "device_online": bool(dev and dev.conn)})
            elif channel.label == "stability":
                viewer.stability = channel
                log.info("viewer %s: stability channel ordered=%s maxRetransmits=%s",
                         viewer.id, channel.ordered, channel.maxRetransmits)
            session.viewers.add(viewer)

        @pc.on("connectionstatechange")
        async def on_state():
            log.info("viewer %s (user %s, session %s): %s", viewer.id, viewer.user_id, session.id, pc.connectionState)
            if pc.connectionState in ("failed", "closed"):
                session.viewers.discard(viewer)
                self.peers.discard(pc)
                await pc.close()

        await pc.setRemoteDescription(offer)
        await pc.setLocalDescription(await pc.createAnswer())   # gathers ALL candidates here
        return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})

    async def _close_all(self, app=None):
        for pc in list(self.peers):
            await pc.close()
        self.peers.clear()
