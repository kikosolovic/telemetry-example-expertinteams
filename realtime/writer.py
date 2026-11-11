"""Telemetry writer: validated TCP batches -> TimescaleDB (SCRUM-153/154/184).

Uses psycopg 3's async pool directly instead of the Django ORM:
  * a batch is hundreds of rows; the ORM's per-object overhead buys nothing
  * the ORM's connection handling assumes short request cycles, and this
    process runs for weeks
  * it's fully async, so a slow insert never blocks the UDP path

Dedup: `INSERT ... ON CONFLICT DO NOTHING` against the unique index
(device_id, boot_id, seq, ts). Resending a batch is therefore always safe,
which is what lets the board's recovery logic be dumb: "after reconnecting,
resend everything after the last ACK".

(COPY would be faster but doesn't support ON CONFLICT. At 100 Hz per board,
executemany in pipeline mode is plenty.)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from django.conf import settings
from psycopg_pool import AsyncConnectionPool

from boards.models import Session
from boards.telemetry_queries import GAPS_SQL

log = logging.getLogger("realtime.writer")

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

INSERT_SQL = """
INSERT INTO telemetry_sample
    (ts, clock_offset_us, session_id, device_id, boot_id, seq,
     lc_fl, lc_fr, lc_bl, lc_br, roll, pitch, actuator, mode, flags)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
"""


def conninfo_from_django() -> str:
    db = settings.DATABASES["default"]
    return (f"host={db['HOST']} port={db['PORT']} dbname={db['NAME']} "
            f"user={db['USER']} password={db['PASSWORD']}")


def board_time(t_us: int) -> datetime:
    # Exact integer arithmetic (no float rounding): the same t_us must always
    # map to the same timestamptz, or the dedup index would miss resends.
    return EPOCH + timedelta(microseconds=t_us)


class TelemetryWriter:
    def __init__(self):
        self.pool = AsyncConnectionPool(conninfo_from_django(), min_size=1, max_size=4, open=False)

    async def open(self):
        await self.pool.open(wait=True)

    async def close(self):
        await self.pool.close()

    async def insert(self, rows: list[tuple]) -> int:
        """Insert rows; returns how many were *new* (the rest were duplicates).
        Returns only after COMMIT, so the caller may ACK the board afterwards:
        an ACK must never cover data that isn't durable yet."""
        if not rows:
            return 0
        async with self.pool.connection() as conn:   # commits on clean exit
            async with conn.cursor() as cur:
                await cur.executemany(INSERT_SQL, rows)
                return cur.rowcount

    async def finalise_session(self, session_id: int) -> list[dict]:
        """Called once the grace period after a session ended has passed.
        Whatever seq ranges are still missing now will never arrive: write them
        to the session as its gap flag (SCRUM-153)."""
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(GAPS_SQL, (session_id,))
                gaps = [{"boot_id": b, "from": f, "to": t, "missing": t - f + 1}
                        for b, f, t in await cur.fetchall()]
        await Session.objects.filter(pk=session_id).aupdate(gaps=gaps)
        log.info("session %s finalised: %d gap(s) %s", session_id, len(gaps),
                 ", ".join(f"{g['from']}-{g['to']}" for g in gaps) or "")
        return gaps
