from django.urls import re_path

from apps.chats.consumers import ChatConsumer

websocket_urlpatterns = [
    re_path(r'ws/chat/(?P<chat_id>[0-9a-f-]+)/$', ChatConsumer.as_asgi()),
]
