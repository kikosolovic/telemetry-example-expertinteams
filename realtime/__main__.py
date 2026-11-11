"""Entry point:  python -m realtime

Same code base / Docker image as Django, different command: Django runs under
uvicorn/gunicorn, this runs as one long-lived asyncio process. In production
it gets its own container with `network_mode: host` so aiortc advertises the
VPS's public IP and its random ICE ports are reachable (spike Q1, "Docker
networking").
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    # Use Django's settings + ORM (for devices/sessions) without its request
    # machinery. django.setup() must run before importing models.
    sys.path.insert(0, str(ROOT / "web"))
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "telemetry_site.settings")
    import django
    django.setup()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-18s %(message)s", datefmt="%H:%M:%S",
                        force=True)
    for noisy in ("aioice", "aiortc", "psycopg.pool"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    from realtime.service import Service

    # Windows: psycopg's async mode needs a selector event loop (the default
    # Proactor loop lacks add_reader). Selector supports UDP and TCP servers
    # too, so one loop runs everything. On Linux the default loop is fine.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        asyncio.run(Service().run(), loop_factory=loop_factory)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
