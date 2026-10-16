from django.urls import path

from . import views

urlpatterns = [
    path("health", views.health),
    path("health/", views.health),
    path("actions", views.actions),
    path("actions/", views.actions),
    path("brief", views.shift_brief),
    path("brief/", views.shift_brief),
    path("ai/ping", views.ai_ping),
    path("vehicles", views.vehicles),
]
