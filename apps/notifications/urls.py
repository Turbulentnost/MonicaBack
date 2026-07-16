from django.urls import path

from apps.notifications.views import RegisterDeviceView, UnregisterDeviceView

urlpatterns = [
    path('devices/', RegisterDeviceView.as_view()),
    path('devices/unregister/', UnregisterDeviceView.as_view()),
]
