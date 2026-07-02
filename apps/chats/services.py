from django.contrib.auth import get_user_model
from django.db.models import Q

from apps.chats.models import Chat, ChatParticipant, Message

User = get_user_model()


def get_or_create_direct_chat(user_a, user_b):
    if user_a.id == user_b.id:
        raise ValueError('Нельзя создать чат с самим собой')

    existing = Chat.objects.filter(
        participants__user=user_a
    ).filter(
        participants__user=user_b
    ).distinct().first()

    if existing:
        return existing, False

    chat = Chat.objects.create()
    ChatParticipant.objects.create(chat=chat, user=user_a)
    ChatParticipant.objects.create(chat=chat, user=user_b)
    return chat, True


def get_user_chats(user):
    return Chat.objects.filter(participants__user=user).distinct()


def get_chat_partner(chat, user):
    participant = chat.participants.exclude(user=user).select_related('user').first()
    return participant.user if participant else None


def user_in_chat(chat, user):
    return chat.participants.filter(user=user).exists()
