"""Django views: dashboard, session start/stop, live page, viewer token.

Note what is *absent*: no view touches packets, sockets or WebRTC. Starting a
session is a DB write plus a NOTIFY; the `realtime` service reacts to it.
"""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import IntegrityError, transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Device, Session
from .notify import notify_realtime
from .telemetry_queries import session_gaps, session_stats

# Salt namespaces the signature, so a token signed for some other purpose
# (password reset, etc.) can never be replayed as a viewer token.
RTC_TOKEN_SALT = "rtc-viewer"
RTC_TOKEN_MAX_AGE = 60  # seconds; the page fetches a fresh one per (re)connect


@login_required
def dashboard(request):
    devices = Device.objects.filter(owner=request.user).order_by("pk")
    sessions = list(Session.objects.filter(trainer=request.user).select_related("device")[:10])
    for s in sessions:
        # Stored-record view straight from the hypertable: lets you see the TCP
        # batches landing and gaps being healed after a Wi-Fi drop.
        s.stats = session_stats(s.pk)
        s.live_gaps = s.gaps if s.gaps is not None else session_gaps(s.pk)
    return render(request, "boards/dashboard.html", {"devices": devices, "sessions": sessions})


@login_required
@require_POST
def start_session(request):
    device = get_object_or_404(Device, pk=request.POST.get("device"), owner=request.user)
    try:
        # One transaction: the INSERT and the NOTIFY commit together, or not at all.
        with transaction.atomic():
            session = Session.objects.create(trainer=request.user, participant=request.user, device=device)
            notify_realtime("session_started", session=session.pk, device=device.pk)
    except IntegrityError:
        # The partial unique constraint fired: this board already has a running session.
        messages.error(request, f"{device} already has a running session.")
        return redirect("dashboard")
    return redirect("live", session_id=session.pk)


@login_required
@require_POST
def stop_session(request, session_id):
    with transaction.atomic():
        session = get_object_or_404(Session.objects.select_for_update(), pk=session_id, trainer=request.user)
        if session.status == Session.RUNNING:
            session.status = Session.ENDED
            session.ended_at = timezone.now()
            session.save(update_fields=["status", "ended_at"])
            notify_realtime("session_ended", session=session.pk, device=session.device_id)
    return redirect("dashboard")


@login_required
def live(request, session_id):
    session = get_object_or_404(Session.objects.select_related("device"), pk=session_id)
    if not session.can_view(request.user):
        raise Http404
    return render(request, "boards/live.html", {
        "session": session,
        "realtime_url": settings.REALTIME_PUBLIC_URL,
        "ice_servers": settings.RTC_ICE_SERVERS,
    })


@login_required
def rtc_token(request, session_id):
    """Short-lived, signed proof that *this user* may watch *this session*.

    The browser sends it with its WebRTC offer to `realtime`, which checks it
    with the same SECRET_KEY (django.core.signing) -- no DB round trip and no
    shared cookie needed across ports. Behind a same-origin reverse proxy you
    could instead let `realtime` read Django's session cookie directly from the
    session table; the token is simpler and works cross-origin in dev.
    """
    session = get_object_or_404(Session, pk=session_id)
    if not session.can_view(request.user):
        raise Http404
    token = signing.dumps({"uid": request.user.pk, "sid": session.pk}, salt=RTC_TOKEN_SALT)
    return JsonResponse({"token": token})


@login_required
def session_status(request, session_id):
    """Polled by the live page for the stored-record panel (not the live path)."""
    session = get_object_or_404(Session, pk=session_id)
    if not session.can_view(request.user):
        raise Http404
    return JsonResponse({
        "status": session.status,
        "stats": session_stats(session.pk),
        "gaps": session.gaps if session.gaps is not None else session_gaps(session.pk),
        "gaps_final": session.gaps is not None,
    })
