"""UDP listener: board live packet -> viewers' data channels (SCRUM-152/178/179).

This is the latency-critical path. Everything in `datagram_received` is
synchronous: no `await`, no DB, no other process. A packet goes from the
socket to `channel.send()` in well under a millisecond.

Every check drops the packet *silently* (just a counter): an attacker probing
the public port learns nothing, and a legit board doesn't care about one lost
live sample -- the full record arrives over TCP anyway.
"""

from __future__ import annotations

import asyncio
import json
import logging

from protocol import telemetry as t

from .registry import Registry, now_us

log = logging.getLogger("realtime.udp")


class UdpListener(asyncio.DatagramProtocol):
    def __init__(self, registry: Registry):
        self.registry = registry
        self.dropped: dict[str, int] = {}

    def _drop(self, reason: str, dev=None) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1
        if dev is not None:
            dev.udp_dropped += 1

    def datagram_received(self, data: bytes, addr) -> None:
        recv_us = now_us()                       # stamp first, before any work

        # 1. Cheap structural checks: size, version, type, count.
        #    A live packet is exactly 56 bytes, so most junk dies here.
        if len(data) != t.LIVE_PACKET_SIZE:
            return self._drop("size")
        try:
            h = t.peek_header(data)
        except t.ProtocolError:
            return self._drop("header")
        if h.msg_type != t.MSG_LIVE:
            return self._drop("type")

        # 2. Unknown device -> drop (SCRUM-151 "requests from unknown devices are rejected").
        dev = self.registry.devices.get(h.device_id)
        if dev is None:
            return self._drop("unknown_device")

        # 3. Authenticate: HMAC tag keyed by this device's token.
        if not t.verify_tag(dev.key, data[:-t.TAG_LEN], data[-t.TAG_LEN:]):
            return self._drop("bad_tag", dev)

        # 4. The boot must match the one the board announced in its
        #    authenticated TCP HELLO. This blocks replays of packets captured
        #    during an earlier boot (their seq numbers might look "new").
        if h.boot_id != dev.boot_id:
            return self._drop("wrong_boot", dev)

        s = t.Sample.unpack_from(data, t.HEADER.size)

        # 5. Stale or duplicate: UDP may reorder or duplicate. A live view only
        #    ever wants *newer* data, so anything not newer is skipped -- it
        #    never blocks the stream (SCRUM-152 "skipped without blocking").
        if s.seq <= dev.last_live_seq:
            return self._drop("stale", dev)
        dev.last_live_seq = s.seq

        # 6. Session matching happens on the server; the board only knows its
        #    own device_id. One running session per board -> one lookup.
        session = self.registry.running_session(dev.id)
        if session is None:
            return self._drop("no_session", dev)
        dev.udp_ok += 1

        # 7. Latency board -> server, using the PING/PONG-corrected board clock.
        offset = dev.clock.offset_us
        if offset is not None:
            session.latency.add((recv_us - (s.t_us - offset)) / 1000.0)

        # 8. CoP + rolling score (O(1) per sample, see stability.py).
        cop = t.centre_of_pressure(s.lc)
        score = session.stability.add(cop, s.roll, s.pitch)

        if not session.viewers:
            return

        # 9. Message format from the spike (SCRUM-80: type + payload), compact
        #    JSON ~110 bytes. `t` is server-corrected ms so clients could plot
        #    it on the same time axis as transcript events. `boot` lets the
        #    client reset its stale-seq check after a board reboot.
        t_ms = (s.t_us - (offset or 0)) / 1000.0
        msg = json.dumps({
            "type": "stability", "v": 1, "seq": s.seq, "boot": h.boot_id, "t": round(t_ms),
            "cop": [round(cop[0], 1), round(cop[1], 1)],
            "roll": round(s.roll, 2), "pitch": round(s.pitch, 2), "score": round(score, 1),
        }, separators=(",", ":"))

        # 10. Fan out. Plain loop over this session's viewers -- this is the
        #     "dict lookup and channel.send()" that makes the one-process
        #     design fast. Copy the set: a viewer may disconnect mid-loop.
        for viewer in list(session.viewers):
            viewer.send_stability(msg, h.boot_id, s.seq)

    def error_received(self, exc) -> None:
        # On Windows, ICMP "port unreachable" from an earlier send can surface
        # here. A listener can safely ignore it.
        log.debug("udp error: %s", exc)
