"""`realtime` -- the one asyncio process that owns all real-time networking.

Spike SCRUM-199 recommendation, implemented as a runnable example:

                         +------------------------- realtime (this package) ----------------------+
    ESP32 --UDP 9000---> | udp.py      verify HMAC, drop stale seq, CoP + rolling score ---+      |
      56 B live pkts     |                                                                   |      |
                         |                                                    dict lookup,   v      |
    ESP32 <--TCP 9001--> | gateway.py  HELLO/NONCE auth, STATE, BATCH->ACK,   no broker  rtc.py  ----WebRTC--> browser
      (TLS in prod)      |             PING/PONG clock sync, online status        aiortc peers,     |  "stability" (unordered,
                         |                 |                                      POST /rtc/offer   |   no retransmits)
                         |                 v                                                        |  "events" (reliable)
                         | writer.py   psycopg 3 async pool -> TimescaleDB (ON CONFLICT DO NOTHING)  |
                         | registry.py in-memory device/session maps <---- listen.py LISTEN realtime |
                         +---------------------------------------------------------------------^----+
                                                                                               |
    Django (web/) --- normal web app: auth, devices, sessions --- NOTIFY realtime '{...}' ------+

Why one process: a live packet goes socket -> data channel inside one event
loop, so forwarding costs a dict lookup and a `channel.send()` -- no Redis hop
inside the 50 ms budget. Django never touches packets.

Run it with:  python -m realtime
"""
