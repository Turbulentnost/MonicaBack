from django.urls import path

from apps.chats.views import (
    ChatBackgroundView,
    ChatListView,
    ChatFilesView,
    ChatMessageDeleteView,
    ChatMessageForwardView,
    ChatMessageRunView,
    ChatMessagesView,
    ChatMessageUploadView,
    MediaProxyView,
    StartChatView,
    UserSearchView,
)
from apps.chats.views_call import (
    AcceptCallView,
    ActiveCallView,
    CallMediaModeView,
    CancelCallView,
    HangupCallView,
    IceConfigView,
    RejectCallView,
    StartCallView,
)
from apps.chats.views_link_preview import LinkPreviewView
from apps.notifications.views import (
    PrivateSessionAcceptView,
    PrivateSessionCloseView,
    PrivateSessionDeclineView,
    PrivateSessionInviteView,
    PrivateSessionLeaveView,
)
from apps.users.views import UserAvatarView

urlpatterns = [
    path('chats/', ChatListView.as_view()),
    path('chats/start/', StartChatView.as_view()),
    path('chats/<uuid:chat_id>/files/', ChatFilesView.as_view()),
    path('chats/<uuid:chat_id>/background/', ChatBackgroundView.as_view()),
    path('chats/<uuid:chat_id>/calls/start/', StartCallView.as_view()),
    path('chats/<uuid:chat_id>/messages/', ChatMessagesView.as_view()),
    path('chats/<uuid:chat_id>/messages/forward/', ChatMessageForwardView.as_view()),
    path('chats/<uuid:chat_id>/messages/upload/', ChatMessageUploadView.as_view()),
    path('chats/<uuid:chat_id>/messages/<uuid:message_id>/run/', ChatMessageRunView.as_view()),
    path('chats/<uuid:chat_id>/messages/<uuid:message_id>/', ChatMessageDeleteView.as_view()),
    path('chats/<uuid:chat_id>/private/invite/', PrivateSessionInviteView.as_view()),
    path('private/leave/', PrivateSessionLeaveView.as_view()),
    path('private/<uuid:session_id>/accept/', PrivateSessionAcceptView.as_view()),
    path('private/<uuid:session_id>/decline/', PrivateSessionDeclineView.as_view()),
    path('private/<uuid:session_id>/close/', PrivateSessionCloseView.as_view()),
    path('calls/<uuid:call_id>/accept/', AcceptCallView.as_view()),
    path('calls/<uuid:call_id>/reject/', RejectCallView.as_view()),
    path('calls/<uuid:call_id>/cancel/', CancelCallView.as_view()),
    path('calls/<uuid:call_id>/hangup/', HangupCallView.as_view()),
    path('calls/<uuid:call_id>/media-mode/', CallMediaModeView.as_view()),
    path('calls/active/', ActiveCallView.as_view()),
    path('calls/ice-config/', IceConfigView.as_view()),
    path('users/search/', UserSearchView.as_view()),
    path('users/<uuid:user_id>/avatar/', UserAvatarView.as_view()),
    path('link-preview/', LinkPreviewView.as_view()),
    path('media/', MediaProxyView.as_view()),
]
