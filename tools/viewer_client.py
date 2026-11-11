"""Headless WebRTC viewer -- does exactly what live.js does, from Python.

Useful to test/demo the live path without a browser, and as the seed of the
SCRUM-144 load test (start N of these to simulate N viewers).

    python tools/viewer_client.py --session 1 --seconds 20

It mints its own viewer token with Django's signing (same SECRET_KEY), so it
must run on a machine with the project settings. A real client gets the token
from Django's /sessions/<id>/rtc-token/ endpoint after logging in.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import aiohttp
from aiortc import RTCPeerConnection, RTCSessionDescription

ROOT = Path(__file__).resolve().parent.parent


def mint_token(session_id: int) -> str:
    sys.path.insert(0, str(ROOT / "web"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "telemetry_site.settings")
    import django
    django.setup()
    from django.core import signing
    from boards.models import Session
    from boards.views import RTC_TOKEN_SALT
    s = Session.objects.get(pk=session_id)
    return signing.dumps({"uid": s.trainer_id, "sid": s.pk}, salt=RTC_TOKEN_SALT)


async def run(a) -> None:
    token = await asyncio.to_thread(mint_token, a.session)
    pc = RTCPeerConnection()
    # Same two channels as the browser, created before the offer.
    events = pc.createDataChannel("events")
    stability = pc.createDataChannel("stability", ordered=False, maxRetransmits=0)

    st = {"n": 0, "stale": 0, "last_seq": -1, "boot": None, "last": None, "latency": None}

    @stability.on("message")
    def on_stability(raw):
        m = json.loads(raw)
        if m["boot"] != st["boot"]:
            st["boot"], st["last_seq"] = m["boot"], -1
        if m["seq"] <= st["last_seq"]:
            st["stale"] += 1
            return
        st["last_seq"] = m["seq"]
        st["n"] += 1
        st["last"] = m

    @events.on("message")
    def on_event(raw):
        m = json.loads(raw)
        if m["type"] == "latency":
            st["latency"] = m
        else:
            print(f"  [events] {m}")

    await pc.setLocalDescription(await pc.createOffer())   # aiortc: gathers candidates here
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{a.url}/rtc/offer", json={"sdp": pc.localDescription.sdp,
                                                         "type": pc.localDescription.type, "token": token}) as r:
            if r.status != 200:
                print("offer rejected:", r.status, await r.text())
                return
            answer = await r.json()
    await pc.setRemoteDescription(RTCSessionDescription(**answer))

    t_end = time.monotonic() + a.seconds
    while time.monotonic() < t_end:
        await asyncio.sleep(1)
        m, lat = st["last"], st["latency"]
        if events.readyState == "open" and m and m["seq"] != st.get("echoed"):
            # Echo the newest seq we "drew" -> server measures server->client.
            st["echoed"] = m["seq"]
            events.send(json.dumps({"type": "echo", "seq": m["seq"], "boot": m["boot"]}))
        if m:
            sc = lat and lat["server_client_ms"]
            print(f"msgs/s {st['n']:4d}  stale {st['stale']:3d}  seq {m['seq']:6d}  "
                  f"CoP ({m['cop'][0]:+6.1f},{m['cop'][1]:+6.1f}) mm  score {m['score']:5.1f}  |  "
                  f"board->server p50 {lat['board_server_p50_ms'] if lat else 0:5.1f} ms  "
                  f"server->client {sc if sc is not None else float('nan'):5.1f} ms  "
                  f"clock offset {lat['clock_offset_ms'] if lat and lat['clock_offset_ms'] is not None else float('nan'):7.1f} ms")
        else:
            print(f"connection {pc.connectionState}, no stability data yet")
        st["n"] = 0
    await pc.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--session", type=int, required=True)
    p.add_argument("--url", default="http://127.0.0.1:8080")
    p.add_argument("--seconds", type=float, default=15)
    a = p.parse_args()
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(run(a), loop_factory=loop_factory)


if __name__ == "__main__":
    main()
