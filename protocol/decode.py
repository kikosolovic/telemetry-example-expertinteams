"""Decoder script for the binary format (SCRUM-148: "documented byte table and a
decoder script so the format stays easy to read while debugging").

Usage:
    python -m protocol.decode <hex>                  # decode a hex dump
    python -m protocol.decode <hex> --token demo-token-1   # ...and verify the tag
    python -m protocol.decode --listen 9100          # print every UDP packet sent to a port

Point the simulator (or a real board) at the --listen port to see packets
exactly as they arrive on the wire.
"""

from __future__ import annotations

import argparse
import socket
import sys

from protocol import telemetry as t


def describe(data: bytes, key: bytes | None) -> str:
    try:
        h = t.peek_header(data)
    except t.ProtocolError as e:
        return f"INVALID ({len(data)} B): {e}"
    kind = {t.MSG_LIVE: "live", t.MSG_BATCH: "batch"}[h.msg_type]
    lines = [f"{kind} v{h.version} device={h.device_id} boot={h.boot_id:#010x} "
             f"count={h.count} size={len(data)} B tag={data[-t.TAG_LEN:].hex()}"]
    if key is not None:
        ok = t.verify_tag(key, data[:-t.TAG_LEN], data[-t.TAG_LEN:])
        lines[0] += "  tag " + ("OK" if ok else "BAD")
    for i in range(min(h.count, 5)):  # first 5 samples is enough for a batch
        s = t.Sample.unpack_from(data, t.HEADER.size + i * t.SAMPLE.size)
        cop = t.centre_of_pressure(s.lc)
        lines.append(
            f"  seq={s.seq} t_us={s.t_us} lc=[{', '.join(f'{v:.2f}' for v in s.lc)}] "
            f"roll={s.roll:+.2f} pitch={s.pitch:+.2f} mode={s.mode} flags={s.flags:#06x} "
            f"-> CoP=({cop[0]:+.1f}, {cop[1]:+.1f}) mm")
    if h.count > 5:
        lines.append(f"  ... {h.count - 5} more")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("hex", nargs="?", help="message as hex")
    p.add_argument("--token", help="device token, to verify the HMAC tag")
    p.add_argument("--listen", type=int, metavar="PORT", help="listen for UDP packets on PORT")
    a = p.parse_args()
    key = a.token.encode() if a.token else None
    if a.listen:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", a.listen))
        print(f"listening on udp/{a.listen} ...")
        while True:
            data, addr = sock.recvfrom(2048)
            print(f"{addr[0]}:{addr[1]}  " + describe(data, key))
    elif a.hex:
        print(describe(bytes.fromhex(a.hex), key))
    else:
        p.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
