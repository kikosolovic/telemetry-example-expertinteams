"""LISTEN side of Django -> realtime events (pairs with web/boards/notify.py).

One dedicated autocommit connection runs `LISTEN realtime` and yields every
notification. If the connection breaks we may have missed events, so after
every (re)connect the whole registry is reloaded *after* LISTEN is active --
that ordering guarantees nothing slips through the gap.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

import psycopg

from boards.notify import CHANNEL

log = logging.getLogger("realtime.listen")


async def listen_forever(conninfo: str, on_connected: Callable[[], Awaitable[None]],
                         on_event: Callable[[dict], Awaitable[None]]) -> None:
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as conn:
                await conn.execute(f"LISTEN {CHANNEL}")
                log.info("listening on channel %r", CHANNEL)
                await on_connected()                    # full reload, after LISTEN
                async for n in conn.notifies():
                    try:
                        event = json.loads(n.payload)
                        log.info("NOTIFY %s", event)
                        await on_event(event)
                    except Exception:
                        log.exception("failed to handle notification %r", n.payload)
        except (psycopg.OperationalError, OSError) as e:
            log.warning("LISTEN connection lost (%s); retrying in 2 s", e)
            await asyncio.sleep(2)
