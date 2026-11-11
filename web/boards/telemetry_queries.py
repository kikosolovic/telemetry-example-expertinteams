"""Read-side SQL on the telemetry hypertable, shared by Django pages and `realtime`.

Kept as plain SQL because the hypertable isn't a Django model (see models.py).
"""

from django.db import connection

# Gap detection (SCRUM-153/184). `seq` increments by exactly 1 per sample
# within one boot, so any jump larger than 1 between consecutive stored rows is
# a hole. LEAD() looks at the next row in seq order inside each boot.
GAPS_SQL = """
SELECT boot_id, seq + 1 AS gap_from, next_seq - 1 AS gap_to
FROM (
    SELECT boot_id, seq, LEAD(seq) OVER (PARTITION BY boot_id ORDER BY seq) AS next_seq
    FROM telemetry_sample
    WHERE session_id = %s
) s
WHERE next_seq - seq > 1
ORDER BY boot_id, gap_from
"""

STATS_SQL = """
SELECT count(*), count(DISTINCT boot_id), min(seq), max(seq)
FROM telemetry_sample WHERE session_id = %s
"""


def session_gaps(session_id: int) -> list[dict]:
    with connection.cursor() as cur:
        cur.execute(GAPS_SQL, [session_id])
        return [{"boot_id": b, "from": f, "to": t, "missing": t - f + 1} for b, f, t in cur.fetchall()]


def session_stats(session_id: int) -> dict:
    with connection.cursor() as cur:
        cur.execute(STATS_SQL, [session_id])
        n, boots, lo, hi = cur.fetchone()
    return {"samples": n, "boots": boots, "min_seq": lo, "max_seq": hi}
