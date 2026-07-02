from django.urls import path

from apps.chats.views import ChatListView, ChatMessagesView, StartChatView, UserSearchView

urlpatterns = [
    path('chats/', ChatListView.as_view()),
    path('chats/start/', StartChatView.as_view()),
    path('chats/<uuid:chat_id>/messages/', ChatMessagesView.as_view()),
    path('users/search/', UserSearchView.as_view()),
]
