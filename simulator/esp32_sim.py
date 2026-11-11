"""ESP32 balance-board simulator (SCRUM-158), speaking the exact v1 protocol.

It behaves like the firmware should (SCRUM-149/177/181/182):

  * Keeps ONE TCP connection to `realtime`: NONCE -> HELLO, then waits for
    STATE. It never knows the session -- it just obeys stream_on/off.
  * While streaming, samples at a fixed rate (100 Hz). Every sample:
      - is packed once (36 bytes) and appended to a ring buffer,
      - is sent immediately as its own 56-byte UDP live packet (no bundling:
        bundling would make each sample wait for the next one).
  * Every batch interval, sends all buffered samples newer than the last one
    sent as a TCP BATCH. On ACK, frees the buffer up to the ACKed seq.
  * Answers PING with PONG (its own clock), so the server can measure offset.
  * Wi-Fi drop: both UDP and TCP go dark, sampling continues into the buffer.
    On reconnect: HELLO again, then resend everything after the last ACK.
    The server dedups, so resending too much is harmless.

Examples:
    python simulator/esp32_sim.py                                   # sway, 100 Hz
    python simulator/esp32_sim.py --motion still
    python simulator/esp32_sim.py --loss 0.2                        # 20 % UDP loss
    python simulator/esp32_sim.py --wifi-drop-at 15 --wifi-drop-for 8
    python simulator/esp32_sim.py --clock-offset-ms 250             # badly synced clock
    python simulator/esp32_sim.py --ack-loss 0.5 --wifi-drop-at 15  # lost ACKs -> duplicate resends
    python simulator/esp32_sim.py --buffer-samples 300 --wifi-drop-at 15   # buffer overflow -> real gap
    python simulator/esp32_sim.py --udp-port 9100                   # point at the decoder

A real ESP32 does the same with WiFiUDP / WiFiClientSecure, mbedTLS HMAC,
WiFi.setSleep(false), and a PSRAM (or LittleFS) ring buffer.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import socket
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # for `protocol`

from protocol import telemetry as t  # noqa: E402

FIRMWARE_VERSION = 3


class Board:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.key = a.token.encode()
        self.device_id = a.device_id
        # New random boot id every boot: seq restarts at 0, and (boot_id, seq)
        # keeps samples from different boots apart.
        self.boot_id = random.getrandbits(32)
        self.seq = 0
        self.clock_offset_us = int(a.clock_offset_ms * 1000)

        # Ring buffer of (seq, packed_sample). Bounded like real RAM/PSRAM: if
        # the drop lasts longer than this, the oldest samples are lost and the
        # server flags them as a gap.
        self.buffer: deque[tuple[int, bytes]] = deque(maxlen=a.buffer_samples)
        self.last_acked = -1      # highest seq the server confirmed as stored
        self.last_sent = -1       # highest seq put into a BATCH on this connection

        # Set by STATE from the server
        self.streaming = False
        self.rate_hz = 100
        self.batch_interval = 2.0

        self.wifi_up = True
        self.tcp_writer: asyncio.StreamWriter | None = None
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.setblocking(False)
        self.stats = {"udp_sent": 0, "udp_lost": 0, "batches": 0, "acks": 0}
        self.t0 = time.monotonic()

    # ------------------------------------------------------------------ clocks

    def board_us(self) -> int:
        """Board wall clock in microseconds. On the ESP32 this is the RTC set
        by SNTP (~100 ms accurate); --clock-offset-ms simulates the error."""
        return time.time_ns() // 1000 + self.clock_offset_us

    def log(self, msg: str) -> None:
        print(f"[board {self.device_id} +{time.monotonic() - self.t0:6.1f}s] {msg}", flush=True)

    # ------------------------------------------------------------------ sensor model

    def measure(self) -> t.Sample:
        """Fake load cells + IMU. The CoP moves on a Lissajous-like path when
        swaying, or jitters by a fraction of a mm when standing still, and the
        4 quadrant weights are derived from it so the server's CoP formula
        recovers exactly that path."""
        el = time.monotonic() - self.t0
        if self.a.motion == "sway":
            amp = self.a.sway_mm
            x = amp * math.sin(2 * math.pi * 0.31 * el) + 0.25 * amp * math.sin(2 * math.pi * 1.1 * el)
            y = 0.7 * amp * math.sin(2 * math.pi * 0.23 * el + 1.0)
        else:
            x = y = 0.0
        x += random.gauss(0, 0.3)
        y += random.gauss(0, 0.3)

        w = self.a.weight_kg
        L = t.HALF_SPAN_MM
        fl = w / 4 * (1 - x / L + y / L)
        fr = w / 4 * (1 + x / L + y / L)
        bl = w / 4 * (1 - x / L - y / L)
        br = w / 4 * (1 + x / L - y / L)
        # Tilt follows the CoP a little (a person leaning tips the board).
        roll = x * 0.04 + random.gauss(0, 0.03)
        pitch = y * 0.04 + random.gauss(0, 0.03)

        s = t.Sample(self.board_us(), self.seq, (fl, fr, bl, br), roll, pitch, actuator=0, mode=0, flags=0)
        self.seq += 1
        return s

    # ------------------------------------------------------------------ sampling + UDP

    async def sampler(self) -> None:
        """Fixed-rate loop. Sleeps until an *absolute* next tick, so timing
        doesn't drift by the processing time of each iteration."""
        next_tick = time.monotonic()
        while True:
            if not self.streaming:
                await asyncio.sleep(0.02)
                next_tick = time.monotonic()
                continue
            sample = self.measure()
            packed = sample.pack()
            self.buffer.append((sample.seq, packed))  # the full record, for TCP

            if self.wifi_up:
                if random.random() < self.a.loss:
                    self.stats["udp_lost"] += 1     # simulated radio loss
                else:
                    pkt = t.encode_message(self.key, t.MSG_LIVE, self.device_id, self.boot_id, [packed])
                    try:
                        self.udp.sendto(pkt, (self.a.host, self.a.udp_port))
                        self.stats["udp_sent"] += 1
                    except (BlockingIOError, OSError):
                        self.stats["udp_lost"] += 1

            next_tick += 1.0 / self.rate_hz
            delay = next_tick - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                next_tick = time.monotonic()   # fell behind: don't burst to catch up

    # ------------------------------------------------------------------ TCP

    async def tcp_loop(self) -> None:
        backoff = 0.5
        while True:
            if not self.wifi_up:
                await asyncio.sleep(0.2)
                continue
            try:
                reader, writer = await asyncio.open_connection(self.a.host, self.a.tcp_port)
            except OSError as e:
                self.log(f"TCP connect failed ({e.__class__.__name__}), retry in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5)
                continue
            backoff = 0.5
            try:
                await self.session(reader, writer)
            except (asyncio.IncompleteReadError, ConnectionError, OSError) as e:
                self.log(f"TCP connection lost ({e.__class__.__name__})")
            finally:
                self.tcp_writer = None
                writer.close()
                # Keep `streaming` as it was: a Wi-Fi drop doesn't end the
                # session, so the board keeps sampling into its buffer until
                # the server says stream_off on the next connection.
            await asyncio.sleep(0.2)

    async def session(self, reader, writer) -> None:
        # 1. Challenge: server sends a fresh nonce, we prove we know the token.
        ftype, nonce = await t.read_frame(reader)
        if ftype != t.F_NONCE:
            raise ConnectionError("expected NONCE")
        flags = t.HELLO_FLAG_WIFI_POWERSAVE if self.a.powersave else 0
        writer.write(t.frame(t.F_HELLO, t.hello_payload(self.key, nonce, self.device_id, self.boot_id,
                                                         FIRMWARE_VERSION, flags)))
        await writer.drain()
        self.tcp_writer = writer
        # 2. Anything not ACKed yet must go again. (Some of it may already be
        #    stored if an ACK got lost on the way -- the server dedups.)
        self.last_sent = self.last_acked
        self.log(f"connected, HELLO sent (boot {self.boot_id:#010x}); "
                 f"{self.unacked()} buffered samples to resend")

        batcher = asyncio.create_task(self.batcher(writer))
        try:
            while True:
                ftype, payload = await t.read_frame(reader)
                if ftype == t.F_STATE:
                    on, interval_ms, rate = t.STATE.unpack(payload)
                    if bool(on) != self.streaming:
                        self.log(f"STATE stream_{'on' if on else 'off'} (batch every {interval_ms} ms, {rate} Hz)")
                    self.streaming, self.batch_interval, self.rate_hz = bool(on), interval_ms / 1000, rate
                elif ftype == t.F_ACK:
                    boot, seq = t.ACK.unpack(payload)
                    if random.random() < self.a.ack_loss:
                        # Pretend this ACK never arrived (e.g. Wi-Fi died right
                        # after the server stored the batch). Those samples stay
                        # "unacked" and get resent after the next reconnect --
                        # the server must store them only once.
                        continue
                    if boot == self.boot_id and seq > self.last_acked:
                        self.last_acked = seq
                        # Free the buffer up to the ACK: this memory is safe to reuse.
                        while self.buffer and self.buffer[0][0] <= seq:
                            self.buffer.popleft()
                        self.stats["acks"] += 1
                elif ftype == t.F_PING:
                    # t2 = when we got it, t3 = when we answer (board clock).
                    (t1,) = t.PING.unpack(payload)
                    t2 = self.board_us()
                    writer.write(t.frame(t.F_PONG, t.PONG.pack(t1, t2, self.board_us())))
        finally:
            batcher.cancel()

    def unacked(self) -> int:
        return sum(1 for s, _ in self.buffer if s > self.last_acked)

    async def batcher(self, writer) -> None:
        """Every batch interval, send everything after `last_sent`. Kept under
        a size cap per frame so a long backlog goes up in several batches."""
        while True:
            await asyncio.sleep(self.batch_interval)
            # Flush while streaming and also afterwards, until the buffer is empty.
            while True:
                pending = [p for s, p in self.buffer if s > self.last_sent][: self.a.max_batch]
                if not pending:
                    break
                first_seq = t.SAMPLE.unpack_from(pending[0], 0)[1]
                last_seq = t.SAMPLE.unpack_from(pending[-1], 0)[1]
                msg = t.encode_message(self.key, t.MSG_BATCH, self.device_id, self.boot_id, pending)
                writer.write(t.frame(t.F_BATCH, msg))
                await writer.drain()
                self.last_sent = last_seq
                self.stats["batches"] += 1
                if len(pending) == self.a.max_batch:
                    self.log(f"BATCH seq {first_seq}-{last_seq} (backlog, more to come)")
                else:
                    break

    # ------------------------------------------------------------------ fault injection + status

    async def wifi_chaos(self) -> None:
        if self.a.wifi_drop_at is None:
            return
        await asyncio.sleep(self.a.wifi_drop_at)
        while True:
            self.log(f"*** Wi-Fi DROP for {self.a.wifi_drop_for:.0f} s: UDP and TCP dark, still sampling into the buffer")
            self.wifi_up = False
            if self.tcp_writer:
                # No FIN reaches the server in a real drop; aborting the socket
                # locally is the closest we get. The server notices via its
                # idle timeout or when the board reconnects.
                self.tcp_writer.transport.abort()
            await asyncio.sleep(self.a.wifi_drop_for)
            self.wifi_up = True
            self.log(f"*** Wi-Fi BACK: {self.unacked()} unacked samples in buffer")
            if not self.a.wifi_drop_repeat:
                return
            await asyncio.sleep(self.a.wifi_drop_repeat)

    async def status(self) -> None:
        while True:
            await asyncio.sleep(5)
            s = self.stats
            self.log(f"{'streaming' if self.streaming else 'idle'} | seq {self.seq} | udp sent {s['udp_sent']} "
                     f"lost {s['udp_lost']} | batches {s['batches']} acks {s['acks']} | "
                     f"buffer {len(self.buffer)} (unacked {self.unacked()}) last ACK {self.last_acked}")

    async def run(self) -> None:
        self.log(f"boot {self.boot_id:#010x} -> udp {self.a.host}:{self.a.udp_port}, tcp {self.a.host}:{self.a.tcp_port}"
                 f" | motion={self.a.motion} loss={self.a.loss:.0%} clock offset={self.a.clock_offset_ms} ms")
        await asyncio.gather(self.sampler(), self.tcp_loop(), self.wifi_chaos(), self.status())


def main() -> None:
    p = argparse.ArgumentParser(description="ESP32 balance board simulator",
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--device-id", type=int, default=1)
    p.add_argument("--token", default="demo-token-1", help="device token from pairing (HMAC key)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--udp-port", type=int, default=9000)
    p.add_argument("--tcp-port", type=int, default=9001)
    p.add_argument("--motion", choices=["sway", "still"], default="sway")
    p.add_argument("--sway-mm", type=float, default=30.0, help="sway amplitude")
    p.add_argument("--weight-kg", type=float, default=75.0)
    p.add_argument("--loss", type=float, default=0.0, help="fraction of UDP packets to drop")
    p.add_argument("--ack-loss", type=float, default=0.0, help="fraction of ACKs to ignore (forces duplicate resends)")
    p.add_argument("--wifi-drop-at", type=float, help="seconds after start to drop Wi-Fi")
    p.add_argument("--wifi-drop-for", type=float, default=8.0)
    p.add_argument("--wifi-drop-repeat", type=float, default=0.0, help="repeat drops every N s (0 = once)")
    p.add_argument("--clock-offset-ms", type=float, default=0.0, help="board clock error vs real time")
    p.add_argument("--buffer-samples", type=int, default=6000, help="ring buffer size (6000 = 60 s at 100 Hz)")
    p.add_argument("--max-batch", type=int, default=1000, help="max samples per BATCH frame")
    p.add_argument("--powersave", action="store_true", help="report Wi-Fi modem sleep as ON in HELLO")
    a = p.parse_args()
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        asyncio.run(Board(a).run(), loop_factory=loop_factory)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
