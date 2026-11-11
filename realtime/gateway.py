"""Board TCP gateway (SCRUM-153/183, spike Q2 "Board TCP protocol").

Each board keeps ONE persistent connection (TLS in production) for the whole
time it's powered on. That single connection does four jobs:

  1. Authentication -- NONCE/HELLO challenge with the device token.
  2. Control        -- STATE frames push stream_on/off, batch interval and
                       sample rate *down* to the board. The board never needs
                       to know which session it's in.
  3. The full record -- BATCH up, ACK down. ACK = "stored durably up to seq N,
                       you may free your buffer". After a Wi-Fi drop the board
                       reconnects and resends everything after its last ACK.
  4. Clock sync + presence -- PING/PONG for the clock offset; the connection
                       being open *is* the online status, and its traffic is
                       "last seen" (SCRUM-151).

Session state lives in the same process (registry), which is why this sits
next to the UDP listener: pushing STATE on session start is a local call.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING

from django.utils import timezone

from protocol import telemetry as t

from . import config
from .registry import DeviceState, Registry, now_us
from .writer import TelemetryWriter, board_time

if TYPE_CHECKING:
    from .service import Service

log = logging.getLogger("realtime.gateway")

HELLO_TIMEOUT_S = 10
IDLE_TIMEOUT_S = 15          # no frame at all for this long -> consider the board gone
PING_EVERY_S = 2.0           # after a quick burst of 5 on connect
LAST_SEEN_EVERY_S = 5.0      # throttle "last seen" DB writes


class BoardConnection:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, device: DeviceState):
        self.reader, self.writer, self.device = reader, writer, device
        self.peer = writer.get_extra_info("peername")

    def send(self, ftype: int, payload: bytes = b"") -> None:
        # StreamWriter.write never blocks; it buffers. Frames here are tiny.
        if not self.writer.is_closing():
            self.writer.write(t.frame(ftype, payload))

    def close(self) -> None:
        self.writer.close()


class Gateway:
    def __init__(self, service: "Service"):
        self.service = service
        self.registry: Registry = service.registry
        self.writer: TelemetryWriter = service.writer

    # ------------------------------------------------------------------ control

    def push_state(self, device_id: int) -> None:
        """Tell the board whether to stream. Called on HELLO and whenever
        Django starts/ends a session for this board (via NOTIFY)."""
        dev = self.registry.devices.get(device_id)
        if dev is None or dev.conn is None:
            return  # board offline: it gets STATE on its next HELLO
        stream_on = self.registry.running_session(device_id) is not None
        dev.conn.send(t.F_STATE, t.STATE.pack(int(stream_on), config.BATCH_INTERVAL_MS, config.SAMPLE_RATE_HZ))
        log.info("board %s <- STATE stream_%s", device_id, "on" if stream_on else "off")

    # ------------------------------------------------------------------ connection

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        dev = await self._authenticate(reader, writer)
        if dev is None:
            writer.close()
            return

        conn = BoardConnection(reader, writer, dev)
        if dev.conn is not None:
            # The board reconnected before we noticed its old connection died
            # (typical after a Wi-Fi drop: no FIN ever arrived). Newest wins.
            log.info("board %s: replacing stale connection", dev.id)
            dev.conn.close()
        dev.conn = conn
        log.info("board %s connected from %s (boot %#010x)", dev.id, peer, dev.boot_id)

        self.push_state(dev.id)
        self.service.broadcast_board_status(dev.id, online=True)
        pinger = asyncio.create_task(self._ping_loop(conn))
        last_seen_written = time.monotonic()
        try:
            while True:
                ftype, payload = await asyncio.wait_for(t.read_frame(reader), IDLE_TIMEOUT_S)
                if ftype == t.F_BATCH:
                    await self._handle_batch(conn, payload)
                elif ftype == t.F_PONG:
                    t1, t2, t3 = t.PONG.unpack(payload)
                    dev.clock.add(t1, t2, t3, now_us())
                else:
                    log.warning("board %s: unexpected frame %s", dev.id, t.FRAME_NAMES.get(ftype, ftype))
                if time.monotonic() - last_seen_written > LAST_SEEN_EVERY_S:
                    last_seen_written = time.monotonic()
                    await self.registry.mark_device(dev.id, last_seen=timezone.now())
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError, t.ProtocolError) as e:
            log.info("board %s disconnected: %s", dev.id, type(e).__name__)
        finally:
            pinger.cancel()
            writer.close()
            if dev.conn is conn:          # don't clobber a newer connection
                dev.conn = None
                await self.registry.mark_device(dev.id, online=False, last_seen=timezone.now())
                self.service.broadcast_board_status(dev.id, online=False)

    async def _authenticate(self, reader, writer) -> DeviceState | None:
        """NONCE -> HELLO challenge. The token never crosses the network; the
        board proves it knows it by HMAC-ing a fresh random nonce. Replaying a
        recorded HELLO fails because the nonce differs every time."""
        nonce = os.urandom(t.NONCE_LEN)
        writer.write(t.frame(t.F_NONCE, nonce))
        try:
            ftype, payload = await asyncio.wait_for(t.read_frame(reader), HELLO_TIMEOUT_S)
            if ftype != t.F_HELLO:
                raise t.ProtocolError("expected HELLO")
            device_id, boot_id, fw, flags, fields, mac = t.parse_hello(payload)
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError, t.ProtocolError) as e:
            log.info("handshake failed from %s: %s", writer.get_extra_info("peername"), e)
            return None

        dev = self.registry.devices.get(device_id)
        if dev is None or not t.verify_hello(dev.key, nonce, fields, mac):
            # Same response for "unknown" and "wrong token": don't tell a
            # scanner which device ids exist.
            log.warning("rejected board claiming id %s from %s", device_id, writer.get_extra_info("peername"))
            return None

        if dev.boot_id != boot_id:
            # New boot: seq restarted at 0 and the board re-synced SNTP, so
            # the old stale-seq marker and clock offset no longer apply.
            dev.boot_id = boot_id
            dev.last_live_seq = -1
            dev.clock = type(dev.clock)()
        powersave = bool(flags & t.HELLO_FLAG_WIFI_POWERSAVE)
        if powersave:
            log.warning("board %s has Wi-Fi power save ON: live latency can reach ~300 ms", dev.id)
        await self.registry.mark_device(dev.id, online=True, last_seen=timezone.now(),
                                        firmware=fw, wifi_powersave=powersave)
        return dev

    async def _ping_loop(self, conn: BoardConnection) -> None:
        # A quick burst on connect gets a good clock offset before the first
        # live packets, then a slow steady rate (also acts as a keep-alive).
        n = 0
        while True:
            conn.send(t.F_PING, t.PING.pack(now_us()))
            n += 1
            await asyncio.sleep(0.1 if n < 5 else PING_EVERY_S)

    # ------------------------------------------------------------------ batches

    async def _handle_batch(self, conn: BoardConnection, payload: bytes) -> None:
        dev = conn.device
        try:
            h, samples = t.decode_message(dev.key, payload)    # length, version, tag
        except t.ProtocolError as e:
            log.warning("board %s: invalid batch dropped (%s)", dev.id, e)
            return
        if h.msg_type != t.MSG_BATCH or h.device_id != dev.id or h.boot_id != dev.boot_id:
            log.warning("board %s: batch header mismatch, dropped", dev.id)
            return

        # Assign every sample to a session by *its own time*, not by "what is
        # running now". A batch resent 5 minutes late after a Wi-Fi drop still
        # lands in the session it was measured in -- as long as that session
        # is within its grace period in the registry.
        offset = dev.clock.offset_us or 0.0
        rows, unmatched = [], 0
        for s in samples:
            session = self.registry.session_for_sample(dev.id, s.t_us - offset)
            if session is None:
                unmatched += 1     # e.g. measured just after stream_off
                continue
            rows.append((board_time(s.t_us), round(offset), session.id, dev.id, h.boot_id, s.seq,
                         *s.lc, s.roll, s.pitch, s.actuator, s.mode, s.flags))

        inserted = await self.writer.insert(rows)       # returns after COMMIT

        # ACK only after the commit. If we crash before this line, the board
        # simply resends and ON CONFLICT drops what did get stored.
        last_seq = samples[-1].seq
        conn.send(t.F_ACK, t.ACK.pack(h.boot_id, last_seq))
        log.info("board %s BATCH seq %d-%d: %d new, %d duplicate, %d outside any session -> ACK %d",
                 dev.id, samples[0].seq, last_seq, inserted, len(rows) - inserted, unmatched, last_seq)
