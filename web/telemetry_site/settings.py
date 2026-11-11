"""Django settings for the telemetry demo.

Django is a *normal web app* in this design: pages, auth, devices, sessions.
It never opens a UDP/TCP socket and never sees a packet. All real-time work
lives in the separate `realtime` process (see realtime/__main__.py), which
imports these same settings so it can use the ORM and the SECRET_KEY.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Dev default only. The `realtime` service must use the same value, because it
# verifies the signed viewer tokens Django hands to the browser (see views.py).
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me-in-production-0123456789")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "boards",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "telemetry_site.urls"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

WSGI_APPLICATION = "telemetry_site.wsgi.application"

# One Postgres instance with the TimescaleDB extension holds *both* Django's
# normal tables and the telemetry hypertable (SCRUM-154). Postgres also gives
# us LISTEN/NOTIFY for Django -> realtime events, so no Redis is needed.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("PGDATABASE", "telemetry"),
        "USER": os.environ.get("PGUSER", "telemetry"),
        "PASSWORD": os.environ.get("PGPASSWORD", "telemetry"),
        "HOST": os.environ.get("PGHOST", "127.0.0.1"),
        "PORT": os.environ.get("PGPORT", "5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = []  # demo: allow the "demo" password
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Europe/Copenhagen"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

# Where the browser sends its WebRTC offer. In production a reverse proxy
# (nginx/Caddy) routes POST /rtc/offer on the *same origin* to `realtime`, so
# this would just be "" (relative URL). In the demo the two processes listen on
# different ports, so the page calls realtime directly and realtime sends CORS
# headers.
REALTIME_PUBLIC_URL = os.environ.get("REALTIME_PUBLIC_URL", "http://localhost:8080")

# Optional STUN/TURN servers for the browser (SCRUM-81). Not needed on
# localhost. In production: coturn with TURN over TLS on 443.
RTC_ICE_SERVERS = []

LOGGING = {
    "version": 1,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
