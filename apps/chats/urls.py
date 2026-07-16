from django.urls import path

from apps.chats.views import (
    ChatListView,
    ChatMessageDeleteView,
    ChatMessageRunView,
    ChatMessagesView,
    ChatMessageUploadView,
    MediaProxyView,
    StartChatView,
    UserSearchView,
)
from apps.notifications.views import (
    PrivateSessionAcceptView,
    PrivateSessionCloseView,
    PrivateSessionDeclineView,
    PrivateSessionInviteView,
    PrivateSessionLeaveView,
)

urlpatterns = [
    path('chats/', ChatListView.as_view()),
    path('chats/start/', StartChatView.as_view()),
    path('chats/<uuid:chat_id>/messages/', ChatMessagesView.as_view()),
    path('chats/<uuid:chat_id>/messages/upload/', ChatMessageUploadView.as_view()),
    path('chats/<uuid:chat_id>/messages/<uuid:message_id>/run/', ChatMessageRunView.as_view()),
    path('chats/<uuid:chat_id>/messages/<uuid:message_id>/', ChatMessageDeleteView.as_view()),
    path('chats/<uuid:chat_id>/private/invite/', PrivateSessionInviteView.as_view()),
    path('private/leave/', PrivateSessionLeaveView.as_view()),
    path('private/<uuid:session_id>/accept/', PrivateSessionAcceptView.as_view()),
    path('private/<uuid:session_id>/decline/', PrivateSessionDeclineView.as_view()),
    path('private/<uuid:session_id>/close/', PrivateSessionCloseView.as_view()),
    path('users/search/', UserSearchView.as_view()),
    path('media/', MediaProxyView.as_view()),
]
