from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.chats.models import Chat, ChatParticipant, Message, MessageType
from apps.chats.services import mark_messages_read
from apps.users.models import User


class ReadReceiptCascadeTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(
            email='alice-read@example.com',
            password='password',
            first_name='Alice',
            last_name='R',
            nickname='alice_read',
        )
        self.bob = User.objects.create_user(
            email='bob-read@example.com',
            password='password',
            first_name='Bob',
            last_name='R',
            nickname='bob_read',
        )
        self.chat = Chat.objects.create()
        ChatParticipant.objects.create(chat=self.chat, user=self.alice)
        ChatParticipant.objects.create(chat=self.chat, user=self.bob)

    def test_reading_later_message_marks_earlier_ones(self):
        earlier = Message.objects.create(
            chat=self.chat,
            sender=self.alice,
            message_type=MessageType.TEXT,
            content='earlier',
            sent_at=timezone.now() - timedelta(minutes=2),
        )
        middle = Message.objects.create(
            chat=self.chat,
            sender=self.alice,
            message_type=MessageType.TEXT,
            content='middle',
            sent_at=timezone.now() - timedelta(minutes=1),
        )
        later = Message.objects.create(
            chat=self.chat,
            sender=self.alice,
            message_type=MessageType.TEXT,
            content='later',
        )

        marked = mark_messages_read(self.chat, self.bob, [later.id])
        self.assertCountEqual(marked, [earlier.id, middle.id, later.id])

        earlier.refresh_from_db()
        middle.refresh_from_db()
        later.refresh_from_db()
        self.assertIsNotNone(earlier.read_at)
        self.assertIsNotNone(middle.read_at)
        self.assertIsNotNone(later.read_at)
