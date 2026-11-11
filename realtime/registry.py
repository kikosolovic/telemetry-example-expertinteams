"""In-memory registry: who is who, right now.

The UDP hot path must never wait for the database. So everything a packet
needs -- the device's HMAC key, its current boot, its running session, that
session's viewers -- lives in plain dicts here:

    devices:  device_id  -> DeviceState  (key, boot, TCP connection, clock sync)
    sessions: session_id -> SessionState (time window, viewers, rolling score)

The registry is loaded from Postgres through the Django ORM at start-up and
kept current by LISTEN/NOTIFY events (listen.py). ORM calls happen only on
those events and on board connect/disconnect -- never per packet.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from django.db import close_old_connections
from django.utils import timezone

from boards.models import Device, Session

from . import config
from .stability import RollingStability

if TYPE_CHECKING:
    from .gateway import BoardConnection
    from .rtc import Viewer

log = logging.getLogger("realtime.registry")


def now_us() -> int:
    return time.time_ns() // 1000


def dt_to_us(dt) -> int:
    return int(dt.timestamp() * 1_000_000)


# --------------------------------------------------------------------------
# Clock sync (spike Q3 "Clocks")
# --------------------------------------------------------------------------

class ClockSync:
    """NTP-style offset between the board clock and the server clock.

    The server sends PING(t1). The board answers PONG(t1, t2, t3) where t2 and
    t3 are *board* times at receive and send; the server notes t4 on arrival:

        server:  t1 ---------------------------------> t4
                     \\                              /
        board:        t2 ---- (processing) ---- t3

        rtt    = (t4 - t1) - (t3 - t2)           # network time only
        offset = ((t2 - t1) + (t3 - t4)) / 2     # board clock - server clock

    The offset is exact if both directions take equally long; its error is
    half the asymmetry. Queueing delay only ever *adds* time, so the sample
    with the lowest RTT is the least distorted one -- we keep the last 8 and
    trust the lowest-RTT one. SNTP alone is only ~100 ms accurate on the ESP32;
    this brings it to a few ms, good enough to measure a 50 ms budget.
    """

    def __init__(self, keep: int = 8):
        self.samples: deque[tuple[int, float]] = deque(maxlen=keep)

    def add(self, t1: int, t2: int, t3: int, t4: int) -> None:
        rtt = (t4 - t1) - (t3 - t2)
        offset = ((t2 - t1) + (t3 - t4)) / 2
        self.samples.append((rtt, offset))

    @property
    def offset_us(self) -> float | None:
        if not self.samples:
            return None
        return min(self.samples)[1]

    @property
    def best_rtt_us(self) -> int | None:
        return min(self.samples)[0] if self.samples else None


class LatencyStats:
    """Board -> server latency for the last ~10 s of live packets."""

    def __init__(self, keep: int = 1000):
        self.values: deque[float] = deque(maxlen=keep)

    def add(self, ms: float) -> None:
        self.values.append(ms)

    def percentile(self, p: float) -> float | None:
        if not self.values:
            return None
        s = sorted(self.values)
        return s[min(len(s) - 1, math.floor(p * len(s)))]


# --------------------------------------------------------------------------
# State objects
# --------------------------------------------------------------------------

@dataclass(eq=False)
class DeviceState:
    id: int
    key: bytes                                   # device token = HMAC key
    name: str
    conn: "BoardConnection | None" = None        # live TCP gateway connection
    boot_id: int | None = None                   # from the authenticated HELLO
    last_live_seq: int = -1                      # highest UDP seq accepted this boot
    clock: ClockSync = field(default_factory=ClockSync)
    udp_ok: int = 0                              # counters, for the status log
    udp_dropped: int = 0


@dataclass(eq=False)
class SessionState:
    id: int
    device_id: int
    started_us: int                              # server time
    ended_us: int | None = None
    viewers: set["Viewer"] = field(default_factory=set)
    stability: RollingStability = field(default_factory=lambda: RollingStability(config.STABILITY_WINDOW, config.SAMPLE_RATE_HZ))
    latency: LatencyStats = field(default_factory=LatencyStats)

    @property
    def running(self) -> bool:
        return self.ended_us is None

    def contains(self, t_server_us: float) -> bool:
        # A sample belongs to the session whose time window contains it.
        return self.started_us <= t_server_us and (self.ended_us is None or t_server_us <= self.ended_us)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

async def _fresh_db():
    # A long-running process never goes through Django's request cycle, so
    # Django never recycles its DB connection. Drop dead/expired ones before
    # each burst of ORM work (cheap: it's a no-op when the connection is fine).
    await sync_to_async(close_old_connections)()


class Registry:
    def __init__(self):
        self.devices: dict[int, DeviceState] = {}
        self.sessions: dict[int, SessionState] = {}

    # ---- loading --------------------------------------------------------

    async def load_all(self) -> list[Session]:
        """Full (re)load. Called after LISTEN is active, so no event can fall
        into the gap between "read the DB" and "start listening".
        Returns ended-but-unfinalised sessions so the service can finalise them."""
        await _fresh_db()
        seen = set()
        async for d in Device.objects.all():
            seen.add(d.pk)
            self._upsert_device(d)
        for gone in set(self.devices) - seen:
            self.devices.pop(gone)

        cutoff = timezone.now() - timedelta(seconds=config.SESSION_GRACE_S)
        pending_finalise = []
        async for s in Session.objects.filter(gaps__isnull=True):
            if s.status == Session.ENDED and s.ended_at < cutoff:
                pending_finalise.append(s)          # past grace: finalise now
                continue
            self._upsert_session(s)
            if s.status == Session.ENDED:
                pending_finalise.append(s)          # still within grace
        log.info("registry loaded: %d devices, %d sessions", len(self.devices), len(self.sessions))
        return pending_finalise

    def _upsert_device(self, d: Device) -> DeviceState:
        st = self.devices.get(d.pk)
        if st is None:
            st = self.devices[d.pk] = DeviceState(d.pk, d.token.encode(), d.name)
        else:
            st.key, st.name = d.token.encode(), d.name   # token may have been rotated
        return st

    def _upsert_session(self, s: Session) -> SessionState:
        st = self.sessions.get(s.pk)
        if st is None:
            st = self.sessions[s.pk] = SessionState(s.pk, s.device_id, dt_to_us(s.started_at))
        st.ended_us = dt_to_us(s.ended_at) if s.ended_at else None
        return st

    # ---- events from Django (via NOTIFY) ---------------------------------

    async def refresh_session(self, session_id: int) -> SessionState | None:
        await _fresh_db()
        s = await Session.objects.filter(pk=session_id).afirst()
        if s is None:
            self.sessions.pop(session_id, None)
            return None
        return self._upsert_session(s)

    async def refresh_device(self, device_id: int) -> DeviceState | None:
        await _fresh_db()
        d = await Device.objects.filter(pk=device_id).afirst()
        if d is None:
            self.devices.pop(device_id, None)
            return None
        return self._upsert_device(d)

    # ---- lookups (hot path: pure dict/loop, no awaits) -------------------

    def running_session(self, device_id: int) -> SessionState | None:
        # A handful of sessions per process; a linear scan is fine. Index by
        # device if SCRUM-140's load test says otherwise.
        for s in self.sessions.values():
            if s.device_id == device_id and s.running:
                return s
        return None

    def session_for_sample(self, device_id: int, t_server_us: float) -> SessionState | None:
        for s in self.sessions.values():
            if s.device_id == device_id and s.contains(t_server_us):
                return s
        return None

    # ---- writes back to Django tables ----------------------------------

    async def mark_device(self, device_id: int, **fields) -> None:
        await _fresh_db()
        await Device.objects.filter(pk=device_id).aupdate(**fields)
