"""Devices and sessions -- the small amount of state Django owns.

The high-rate telemetry itself is *not* a Django model. It lives in a
TimescaleDB hypertable created by a raw-SQL migration (0002) and is written
only by the `realtime` service with psycopg's bulk inserts. Django still owns
the migration, so there is one source of truth for the schema.
"""

import secrets

from django.conf import settings
from django.db import models
from django.db.models import Q


def new_device_token() -> str:
    return secrets.token_hex(16)


class Device(models.Model):
    """A balance board (SCRUM-151).

    `id` is the numeric device_id the board puts in every packet header.
    `token` is given to the board over BLE during pairing (SCRUM-150) and is
    the HMAC key for UDP tags and for the TCP HELLO challenge. It is never sent
    over the network by the board. (The server needs the raw key to verify an
    HMAC, so it can't be stored hashed like a password; in production encrypt
    it at rest.)
    """

    name = models.CharField(max_length=100)
    token = models.CharField(max_length=64, default=new_device_token)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="devices")

    # Written by `realtime` when the board's TCP connection opens/closes.
    online = models.BooleanField(default=False)
    last_seen = models.DateTimeField(null=True, blank=True)
    firmware = models.PositiveIntegerField(null=True, blank=True)
    wifi_powersave = models.BooleanField(default=False)  # reported in HELLO

    def __str__(self):
        return f"{self.name} (#{self.pk})"


class Session(models.Model):
    """A training session with one board (SCRUM-62/63, SCRUM-139)."""

    RUNNING, ENDED = "running", "ended"

    trainer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="trained_sessions")
    participant = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="sessions")
    device = models.ForeignKey(Device, on_delete=models.PROTECT, related_name="sessions")
    status = models.CharField(max_length=10, default=RUNNING,
                              choices=[(RUNNING, "Running"), (ENDED, "Ended")])
    # Server time. Samples are assigned to the session whose
    # [started_at, ended_at] window contains the sample's (clock-corrected)
    # time -- not to whatever session is running when a batch arrives.
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    # Filled by `realtime` once the session is final: seq ranges that never
    # arrived, even after resends (SCRUM-153 "gaps flagged on the talk").
    gaps = models.JSONField(null=True, blank=True)

    class Meta:
        constraints = [
            # SCRUM-151: only one running session per board, enforced by the DB.
            models.UniqueConstraint(fields=["device"], condition=Q(status="running"),
                                    name="one_running_session_per_device"),
        ]
        ordering = ["-started_at"]

    def can_view(self, user) -> bool:
        return user.is_staff or user.pk in (self.trainer_id, self.participant_id)

    def __str__(self):
        return f"Session {self.pk} on {self.device}"
