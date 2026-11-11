"""Django -> realtime events via Postgres LISTEN/NOTIFY.

Why NOTIFY:
  * Postgres is already running, so no extra container (Redis) is needed.
  * NOTIFY is *transactional*: it is only delivered when the surrounding
    transaction commits. So `realtime` can never hear "session 7 started"
    for a session that was rolled back, and when it re-reads session 7 from
    the DB the row is guaranteed to be there.

The payload only carries ids. `realtime` re-reads the row through the ORM,
so a payload can never disagree with the database.
"""

import json

from django.db import connection

CHANNEL = "realtime"


def notify_realtime(event: str, **ids) -> None:
    """Queue a notification; Postgres sends it when the current transaction commits."""
    payload = json.dumps({"event": event, **ids})
    with connection.cursor() as cur:
        # pg_notify() instead of the NOTIFY statement, because NOTIFY can't
        # take a bound parameter.
        cur.execute("SELECT pg_notify(%s, %s)", [CHANNEL, payload])
