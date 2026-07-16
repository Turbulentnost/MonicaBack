from django.contrib import admin
from django.urls import include, path

from apps.notifications.urls import device_urlpatterns

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/auth/', include('apps.users.urls')),
    path('api/', include('apps.chats.urls')),
    path('api/notifications/', include('apps.notifications.urls')),
    path('api/devices/', include((device_urlpatterns, 'devices'))),
]
