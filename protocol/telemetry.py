"""Telemetry wire format v1 -- the single source of truth for every byte.

This module implements the layout proposed in the SCRUM-199 spike, section Q3.
The ESP32 firmware (Arduino/C++) must produce exactly these bytes; this Python
version is used by the server (`realtime`), the simulator and the decoder.

Why binary and not JSON?
    The deciding factor is the *board's RAM*, not bandwidth. One sample is
    36 bytes here vs ~220 bytes as JSON. One minute of buffering at 100 Hz
    (needed to survive a Wi-Fi drop, SCRUM-153) is 216 KB binary vs ~1.3 MB
    JSON, and the ESP32's internal RAM can't hold the latter next to Wi-Fi + TLS.

Everything is little-endian (the ESP32 is little-endian too, so the firmware can
`memcpy` a packed struct straight into the send buffer).

    +-------------------- message ---------------------+
    | header (12 B) | sample (36 B) x count | tag (8 B) |
    +--------------------------------------------------+

    UDP live packet  = 1 sample  = 12 + 36 + 8 = 56 bytes, sent every 10 ms
    TCP batch        = N samples = 12 + 36*N + 8 bytes, sent every few seconds

The *same* message layout is used for both, so one decoder handles both
(acceptance criterion of SCRUM-148: "same payload for UDP and TCP").
"""

from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

VERSION = 1

MSG_LIVE = 1   # UDP: exactly one sample, sent as soon as it is measured
MSG_BATCH = 2  # TCP: many samples, the full record that gets stored

# Header: version u8 | msg_type u8 | count u16 | device_id u32 | boot_id u32
#   device_id: numeric id given at registration (SCRUM-151). Never a name.
#   boot_id:   random number picked at every boot. `seq` restarts at 0 after a
#              reboot, so (boot_id, seq) is what makes a sample unique -- a new
#              boot is *not* a duplicate of the previous boot's seq 0..N.
HEADER = struct.Struct("<BBHII")

# Sample: t_us u64 | seq u32 | lc[4] f32 | roll i16 | pitch i16 |
#         actuator u8 | mode u8 | flags u16
#   t_us:  board clock, microseconds since Unix epoch (SNTP-synced, ~100 ms
#          accurate; the server corrects it further with PING/PONG, see below)
#   seq:   +1 per sample at a fixed rate -> gaps are detectable from seq alone
#   lc:    the 4 load-cell quadrants in the order FL, FR, BL, BR.
#          Unit still to be signed off with mechatronics; the demo uses kg.
#   roll/pitch: IMU angles in 0.01 degree steps (i16 -> +/-327 degrees)
#   actuator: actuator state code; mode: 0 = passive, 1 = active
#   flags: bit field, e.g. sensor fault / saturated cell
SAMPLE = struct.Struct("<QI4f2hBBH")

TAG_LEN = 8  # HMAC-SHA256 truncated to 8 bytes (64-bit tag)

assert HEADER.size == 12 and SAMPLE.size == 36  # the doc's byte table

LIVE_PACKET_SIZE = HEADER.size + SAMPLE.size + TAG_LEN  # 56

# Flag bits (examples; extend together with the firmware)
FLAG_SENSOR_FAULT = 1 << 0
FLAG_CELL_SATURATED = 1 << 1


class ProtocolError(ValueError):
    """Raised for anything malformed. The server drops such input silently."""


# --------------------------------------------------------------------------
# Data classes (decoded form)
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Header:
    version: int
    msg_type: int
    count: int
    device_id: int
    boot_id: int


@dataclass(slots=True)
class Sample:
    t_us: int                 # board clock, microseconds since epoch
    seq: int
    lc: tuple[float, float, float, float]   # FL, FR, BL, BR
    roll: float               # degrees (decoded from 0.01 degree steps)
    pitch: float              # degrees
    actuator: int = 0
    mode: int = 0             # 0 passive, 1 active
    flags: int = 0

    def pack(self) -> bytes:
        return SAMPLE.pack(
            self.t_us, self.seq, *self.lc,
            _deg_to_i16(self.roll), _deg_to_i16(self.pitch),
            self.actuator, self.mode, self.flags,
        )

    @classmethod
    def unpack_from(cls, buf: bytes | memoryview, offset: int) -> "Sample":
        t_us, seq, a, b, c, d, roll, pitch, act, mode, flags = SAMPLE.unpack_from(buf, offset)
        return cls(t_us, seq, (a, b, c, d), roll / 100.0, pitch / 100.0, act, mode, flags)


def _deg_to_i16(deg: float) -> int:
    # 0.01 degree resolution, clamped instead of overflowing
    return max(-32768, min(32767, round(deg * 100)))


# --------------------------------------------------------------------------
# HMAC tag
# --------------------------------------------------------------------------
# The VPS is public, so UDP port 9000 is open to the whole internet. Without a
# tag anyone could inject fake balance data into a live session. The key is the
# device token handed to the board during BLE pairing (SCRUM-150/151); the
# ESP32 computes HMAC-SHA256 in hardware via mbedTLS, so this is cheap.
# 8 bytes (64 bits) is plenty for short-lived live packets: a forger would need
# ~2^63 tries per packet, and the server rate-limits nothing because it drops
# bad packets silently at ~microseconds each.

def compute_tag(key: bytes, data: bytes | memoryview) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()[:TAG_LEN]


def verify_tag(key: bytes, data: bytes | memoryview, tag: bytes | memoryview) -> bool:
    # compare_digest = constant time, so timing doesn't leak how many bytes matched
    return hmac.compare_digest(compute_tag(key, data), bytes(tag))


# --------------------------------------------------------------------------
# Encode / decode messages
# --------------------------------------------------------------------------

def encode_message(key: bytes, msg_type: int, device_id: int, boot_id: int,
                   samples_packed: list[bytes] | tuple[bytes, ...]) -> bytes:
    """Build header + samples + tag. `samples_packed` are `Sample.pack()` bytes.

    The board keeps samples *already packed* in its ring buffer, so building a
    batch is just concatenation -- exactly what the firmware does with memcpy.
    """
    if not 1 <= len(samples_packed) <= 0xFFFF:
        raise ProtocolError("count must be 1..65535")
    body = HEADER.pack(VERSION, msg_type, len(samples_packed), device_id, boot_id) + b"".join(samples_packed)
    return body + compute_tag(key, body)


def peek_header(data: bytes | memoryview) -> Header:
    """Parse only the header -- the server needs `device_id` to find the key
    *before* it can verify the tag, and it wants to reject junk cheaply."""
    if len(data) < HEADER.size + SAMPLE.size + TAG_LEN:
        raise ProtocolError("too short")
    h = Header(*HEADER.unpack_from(data, 0))
    if h.version != VERSION:
        raise ProtocolError(f"unsupported version {h.version}")
    if h.msg_type not in (MSG_LIVE, MSG_BATCH):
        raise ProtocolError(f"unknown msg_type {h.msg_type}")
    expected = HEADER.size + SAMPLE.size * h.count + TAG_LEN
    if h.count == 0 or len(data) != expected:
        # Fixed sizes make validation trivial: length must match count exactly.
        raise ProtocolError(f"length {len(data)} != expected {expected}")
    return h


def decode_message(key: bytes, data: bytes | memoryview) -> tuple[Header, list[Sample]]:
    """Validate length, version and tag, then unpack every sample."""
    h = peek_header(data)
    mv = memoryview(data)
    body, tag = mv[:-TAG_LEN], mv[-TAG_LEN:]
    if not verify_tag(key, body, tag):
        raise ProtocolError("bad tag")
    samples = [Sample.unpack_from(mv, HEADER.size + i * SAMPLE.size) for i in range(h.count)]
    return h, samples


# --------------------------------------------------------------------------
# TCP gateway framing (board <-> realtime, one persistent TLS connection)
# --------------------------------------------------------------------------
# Every frame on the stream is:   length u32 | frame_type u8 | payload
# where `length` counts frame_type + payload. A length prefix is the simplest
# way to cut a TCP byte stream back into messages.
#
#   dir   frame   payload
#   S->B  NONCE   16 random bytes, sent right after the connection opens
#   B->S  HELLO   device_id u32, boot_id u32, fw u16, flags u8, hmac[32]
#                 hmac = HMAC-SHA256(token, nonce || the 11 bytes before it)
#                 -> proves the board knows the token without ever sending it,
#                    and the fresh nonce makes a recorded HELLO useless.
#   S->B  STATE   stream_on u8, batch_interval_ms u16, sample_rate_hz u16
#   B->S  BATCH   one MSG_BATCH message (see above)
#   S->B  ACK     boot_id u32, seq u32 = highest seq safely stored; the board
#                 frees its buffer up to there
#   S->B  PING    t1 u64 (server us)
#   B->S  PONG    t1 u64, t2 u64 (board us at receive), t3 u64 (board us at send)
#                 -> NTP-style clock offset, see `ClockSync` in realtime/registry.py

FRAME_LEN = struct.Struct("<I")
MAX_FRAME = 1 + HEADER.size + SAMPLE.size * 0xFFFF + TAG_LEN  # biggest legal batch

F_NONCE, F_HELLO, F_STATE, F_BATCH, F_ACK, F_PING, F_PONG = range(1, 8)
FRAME_NAMES = {F_NONCE: "NONCE", F_HELLO: "HELLO", F_STATE: "STATE", F_BATCH: "BATCH",
               F_ACK: "ACK", F_PING: "PING", F_PONG: "PONG"}

HELLO_FIELDS = struct.Struct("<IIHB")   # device_id, boot_id, fw_version, flags
HELLO_FLAG_WIFI_POWERSAVE = 1 << 0      # board reports if modem sleep is on (kills latency)
STATE = struct.Struct("<BHH")
ACK = struct.Struct("<II")
PING = struct.Struct("<Q")
PONG = struct.Struct("<QQQ")
NONCE_LEN = 16


def frame(ftype: int, payload: bytes = b"") -> bytes:
    return FRAME_LEN.pack(1 + len(payload)) + bytes([ftype]) + payload


async def read_frame(reader) -> tuple[int, bytes]:
    """Read one frame from an asyncio StreamReader. Raises IncompleteReadError on EOF."""
    (length,) = FRAME_LEN.unpack(await reader.readexactly(FRAME_LEN.size))
    if not 1 <= length <= MAX_FRAME:
        raise ProtocolError(f"bad frame length {length}")
    data = await reader.readexactly(length)
    return data[0], data[1:]


def hello_payload(key: bytes, nonce: bytes, device_id: int, boot_id: int,
                  fw_version: int, flags: int) -> bytes:
    fields = HELLO_FIELDS.pack(device_id, boot_id, fw_version, flags)
    return fields + hmac.new(key, nonce + fields, hashlib.sha256).digest()


def parse_hello(payload: bytes) -> tuple[int, int, int, int, bytes, bytes]:
    """Return (device_id, boot_id, fw, flags, signed_fields, mac). Key lookup
    happens afterwards, using device_id."""
    if len(payload) != HELLO_FIELDS.size + 32:
        raise ProtocolError("bad HELLO length")
    fields, mac = payload[:HELLO_FIELDS.size], payload[HELLO_FIELDS.size:]
    return (*HELLO_FIELDS.unpack(fields), fields, mac)


def verify_hello(key: bytes, nonce: bytes, fields: bytes, mac: bytes) -> bool:
    return hmac.compare_digest(hmac.new(key, nonce + fields, hashlib.sha256).digest(), mac)


# --------------------------------------------------------------------------
# Centre of pressure -- shared so simulator and server agree on geometry
# --------------------------------------------------------------------------

# Half the distance between the left and right (and front and back) load
# cells, in mm. Placeholder until mechatronics gives the real board geometry.
HALF_SPAN_MM = 200.0


def centre_of_pressure(lc: tuple[float, float, float, float]) -> tuple[float, float]:
    """CoP in mm from the board centre (SCRUM-155). x = right, y = front.

    Each axis is the weight-weighted average of where the cells sit:
        x = L * ((FR + BR) - (FL + BL)) / total
        y = L * ((FL + FR) - (BL + BR)) / total
    """
    fl, fr, bl, br = lc
    total = fl + fr + bl + br
    if total <= 1e-6:      # nobody on the board
        return 0.0, 0.0
    x = HALF_SPAN_MM * ((fr + br) - (fl + bl)) / total
    y = HALF_SPAN_MM * ((fl + fr) - (bl + br)) / total
    return x, y
