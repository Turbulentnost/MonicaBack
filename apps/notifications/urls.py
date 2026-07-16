from django.urls import path

from apps.notifications.views import (
    NotificationClearView,
    NotificationDeleteView,
    NotificationListView,
    NotificationReadAllView,
    NotificationReadView,
    RegisterDeviceView,
    UnregisterDeviceView,
)

urlpatterns = [
    path('', NotificationListView.as_view()),
    path('read-all/', NotificationReadAllView.as_view()),
    path('clear/', NotificationClearView.as_view()),
    path('<uuid:notification_id>/read/', NotificationReadView.as_view()),
    path('<uuid:notification_id>/', NotificationDeleteView.as_view()),
]

device_urlpatterns = [
    path('', RegisterDeviceView.as_view()),
    path('unregister/', UnregisterDeviceView.as_view()),
]
