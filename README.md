# Board telemetry: working example of the SCRUM-199 design

This is a runnable example of the architecture recommended in the **SCRUM-199 spike: Django + WebRTC and UDP/TCP telemetry**. It has three parts:

- An ESP32 board simulator that streams live samples over UDP and keeps the full record over TCP.
- The `realtime` asyncio service, which holds the UDP listener, the TCP gateway, the aiortc peers and the TimescaleDB writer.
- A Django web app with devices, sessions and a live WebRTC page.

```
 simulator/esp32_sim.py ──UDP 9000 (56 B live packets, HMAC)──────┐
   (= ESP32 firmware)    ──TCP 9001 (NONCE/HELLO, STATE, BATCH/ACK, PING/PONG)──┐
                                                                  ▼            ▼
                                     ┌──────────── realtime (python -m realtime) ─────────────┐
                                     │ udp.py → registry lookup → stability → rtc.Viewer.send │──WebRTC──▶ browser
                                     │ gateway.py → writer.py → TimescaleDB (ON CONFLICT)     │  "stability" unordered,
                                     │ listen.py  ◀── LISTEN realtime                         │   maxRetransmits 0
                                     └────────────────────▲───────────────────────────────────┘  "events" reliable
 web/ (Django) ── start/stop session ── NOTIFY realtime ──┘        POST /rtc/offer (aiohttp + aiortc)
```

## Layout

| Path | What it is | Tickets |
|---|---|---|
| `protocol/telemetry.py` | Binary format v1 (12 B header, 36 B samples, 8 B HMAC tag), TCP framing, CoP | SCRUM-148, 162 |
| `protocol/decode.py` | Decoder script: `python -m protocol.decode <hex>` or `--listen 9100` | SCRUM-148 |
| `realtime/udp.py` | Live path: validate → HMAC → boot/seq check → session → CoP/score → data channel | SCRUM-152, 178, 179 |
| `realtime/gateway.py` | Board TCP gateway: auth, STATE, BATCH→ACK, clock sync, online/last seen | SCRUM-151, 153, 183 |
| `realtime/writer.py` | psycopg 3 async pool → hypertable; gap finalisation | SCRUM-154, 184, 187 |
| `realtime/rtc.py` | aiortc peers, `POST /rtc/offer`, latency echo | SCRUM-64, 78–80, 180 |
| `realtime/registry.py` | In-memory device/session maps, NTP-style `ClockSync` | SCRUM-151 |
| `realtime/listen.py` | Postgres `LISTEN` loop (pairs with `web/boards/notify.py`) | |
| `realtime/stability.py` | O(1) rolling score (placeholder formula `demo-0`) | SCRUM-155, 192 |
| `web/boards/` | Django models, views, migrations (incl. the hypertable), live page (`static/boards/live.js`) | SCRUM-139, 151, 156, 193, 195 |
| `simulator/esp32_sim.py` | Board simulator: sway/still, UDP loss, Wi-Fi drop + resend, lost ACKs, clock skew | SCRUM-158 |
| `tools/viewer_client.py` | Headless WebRTC viewer (also the start of the SCRUM-144 load test) | |

## Run it

Requires Docker and Python 3.12 or later.

```sh
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # (bin/ on Linux/macOS)
docker compose up -d --wait                       # TimescaleDB on 127.0.0.1:5432
.venv/Scripts/python web/manage.py migrate
.venv/Scripts/python web/manage.py demo_setup     # user demo/demo, board #1 with token demo-token-1

# three terminals:
.venv/Scripts/python -m realtime                              # udp/9000, tcp/9001, http/8080
.venv/Scripts/python web/manage.py runserver 127.0.0.1:8000
.venv/Scripts/python simulator/esp32_sim.py                   # the "board"
```

Open http://localhost:8000, log in as `demo` / `demo` and press **Start session**. The page switches to the live view.

Without a browser, run `.venv/Scripts/python tools/viewer_client.py --session <id>`.

### Things to try

| Command | What you'll see |
|---|---|
| `esp32_sim.py --loss 0.2` | Live view keeps flowing at about 80 msg/s; the stored record still has **no gaps** (TCP has everything) |
| `esp32_sim.py --wifi-drop-at 15 --wifi-drop-for 8` | "Live connection lost" banner; afterwards one backlog BATCH resends about 800 samples; no gaps |
| `esp32_sim.py --ack-loss 0.9 --wifi-drop-at 10 --wifi-drop-for 3` | realtime logs `N new, M duplicate`; the unique index drops the resends |
| `esp32_sim.py --buffer-samples 300 --wifi-drop-at 15` | Buffer overflows during the drop; after stop and the 30 s grace, the session gets a gap flag such as `seq 762–1327 (566)` |
| `esp32_sim.py --clock-offset-ms 250` | "Clock offset 250.0 ms" is recovered by PING/PONG; latency stays correct |
| `esp32_sim.py --motion still` | Score about 92 (swaying: about 35) |
| Restart `python -m realtime` while watching | Board and browser reconnect on their own; the running session is reloaded from the DB |
| `esp32_sim.py --udp-port 9100` + `python -m protocol.decode --listen 9100 --token demo-token-1` | Raw packets decoded on the wire |

## Results measured on localhost (2026-11-11)

These numbers are from one laptop. The internet legs to a VPS are **not** included. Per the spike, those still have to be measured from SDU.

- Board → server latency: p50 0.2–0.3 ms, p95 0.5–1.2 ms, with the board clock skewed by 250 ms and corrected by PING/PONG to within 0.1 ms.
- Server → screen (seq echo, includes the wait for the next frame): about 7–8 ms in Chrome and in the aiortc client.
- 5% UDP loss plus a 6 s Wi-Fi drop: 4,852 samples stored (seq 0–4851), 0 gaps; the backlog resend was 861 samples in one batch.
- Lost ACKs: 385 duplicate resends rejected by `ON CONFLICT DO NOTHING`.
- 400-sample buffer with a 6 s drop: a gap of 566 samples was detected and written to `Session.gaps`.

## What differs from production

- **TLS**: the gateway runs plain TCP unless `REALTIME_TLS_CERT` and `REALTIME_TLS_KEY` are set. The ESP32 should use `WiFiClientSecure` with the CA pinned.
- **Same origin**: here the page POSTs to `:8080` with CORS. In production nginx or Caddy routes `/rtc/offer` to `realtime`.
- **Docker**: in production `realtime` runs from the same image as Django with `network_mode: host`, plus coturn on 443 (see `docker-compose.yml`).
- **Grace period**: 30 s here (`REALTIME_SESSION_GRACE_S`); the spike suggests about 10 minutes.
- **Score formula**: `demo-0` is a placeholder until SCRUM-190 is signed off with AllGoodSpeakers.
- **Talks**: the example has sessions only. Talks would be matched by time range in the same way.
