"""All tunables in one place, read from environment variables."""

import os


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


# Bind address. 127.0.0.1 keeps the demo off your LAN; use 0.0.0.0 on the VPS
# (or when a real ESP32 on Wi-Fi should reach your laptop).
BIND = _env("REALTIME_BIND", "127.0.0.1")

HTTP_PORT = int(_env("REALTIME_HTTP_PORT", "8080"))   # POST /rtc/offer
UDP_PORT = int(_env("REALTIME_UDP_PORT", "9000"))     # live packets
TCP_PORT = int(_env("REALTIME_TCP_PORT", "9001"))     # board gateway

# TLS for the board gateway. In production the ESP32 uses WiFiClientSecure
# with this server's CA pinned. The demo runs plain TCP unless both are set.
TLS_CERT = _env("REALTIME_TLS_CERT", "")
TLS_KEY = _env("REALTIME_TLS_KEY", "")

# Told to the board in STATE (SCRUM-153: "batch interval is configurable").
BATCH_INTERVAL_MS = int(_env("REALTIME_BATCH_INTERVAL_MS", "2000"))
SAMPLE_RATE_HZ = int(_env("REALTIME_SAMPLE_RATE_HZ", "100"))

# How long an ended session stays in the registry so late resends (after a
# Wi-Fi drop) still land in it. The spike suggests ~10 minutes in production;
# the demo uses 30 s so you can watch the gaps get finalised.
SESSION_GRACE_S = float(_env("REALTIME_SESSION_GRACE_S", "30"))

# Rolling window for the live stability score (SCRUM-155/192), in samples.
STABILITY_WINDOW = int(_env("REALTIME_STABILITY_WINDOW", "200"))  # 2 s at 100 Hz

# Browser origins allowed to POST offers. In production a reverse proxy puts
# /rtc/offer on the same origin as Django and CORS isn't needed at all.
ALLOWED_ORIGINS = set(_env("REALTIME_ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(","))

# ICE servers for the aiortc side, e.g. "stun:stun.l.google.com:19302".
# Empty is fine on localhost. On the VPS: coturn (SCRUM-81).
ICE_SERVERS = [u for u in _env("REALTIME_ICE_SERVERS", "").split(",") if u]
