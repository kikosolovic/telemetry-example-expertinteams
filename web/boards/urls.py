from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("sessions/start/", views.start_session, name="start_session"),
    path("sessions/<int:session_id>/stop/", views.stop_session, name="stop_session"),
    path("sessions/<int:session_id>/live/", views.live, name="live"),
    path("sessions/<int:session_id>/rtc-token/", views.rtc_token, name="rtc_token"),
    path("sessions/<int:session_id>/status/", views.session_status, name="session_status"),
]
