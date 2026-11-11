"""Create a demo trainer and a demo board so you can run everything locally.

    python web/manage.py demo_setup

Creates user demo/demo and Device #1 with token "demo-token-1" (fixed so the
simulator's default arguments just work). Real boards get a random token.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from boards.models import Device


class Command(BaseCommand):
    help = "Create demo user and board"

    def handle(self, *args, **opts):
        User = get_user_model()
        user, created = User.objects.get_or_create(username="demo", defaults={"is_staff": True, "is_superuser": True})
        if created:
            user.set_password("demo")
            user.save()
        device, _ = Device.objects.update_or_create(
            pk=1, defaults={"name": "Board 1", "owner": user, "token": "demo-token-1"})
        self.stdout.write(self.style.SUCCESS(
            f"login: demo / demo\n"
            f"board: device_id={device.pk} token={device.token}\n"
            f"simulate: python simulator/esp32_sim.py --device-id {device.pk} --token {device.token}"))
