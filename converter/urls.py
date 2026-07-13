from django.urls import path

from . import views

app_name = "converter"

urlpatterns = [
    path("", views.upload_view, name="upload"),
    path("queue/", views.queue_view, name="queue"),
    path("job/<int:job_id>/status/", views.progress_status, name="progress_status"),
]
