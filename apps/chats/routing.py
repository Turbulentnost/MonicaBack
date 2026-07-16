from django.urls import re_path

from apps.chats.consumers import ChatConsumer
from apps.chats.consumers_presence import PresenceConsumer, PrivateSessionConsumer

websocket_urlpatterns = [
    re_path(r'ws/presence/$', PresenceConsumer.as_asgi()),
    re_path(r'ws/private/(?P<session_id>[0-9a-f-]+)/$', PrivateSessionConsumer.as_asgi()),
    re_path(r'ws/chat/(?P<chat_id>[0-9a-f-]+)/$', ChatConsumer.as_asgi()),
]
