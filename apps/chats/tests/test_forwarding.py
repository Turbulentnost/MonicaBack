from unittest.mock import patch

from asgiref.sync import async_to_sync
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase, override_settings
from django.urls import re_path
from rest_framework.test import APIClient

from apps.chats.consumers import ChatConsumer
from apps.chats.models import Chat, ChatParticipant, Message, MessageType
from apps.chats.services import delete_message_for_user
from apps.users.models import User


TEST_CHANNEL_LAYERS = {
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'},
}
TEST_CACHES = {
    'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'},
}


@override_settings(CACHES=TEST_CACHES, CHANNEL_LAYERS=TEST_CHANNEL_LAYERS)
class ForwardingTests(TransactionTestCase):
    def setUp(self):
        self.user = self._user('forwarder')
        self.source_peer = self._user('source_peer')
        self.target_peer = self._user('target_peer')
        self.outsider = self._user('outsider')
        self.source_chat = self._chat(self.user, self.source_peer)
        self.target_chat = self._chat(self.user, self.target_peer)
        self.outsider_chat = self._chat(self.outsider, self.source_peer)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _user(self, nickname):
        return User.objects.create_user(
            email=f'{nickname}@example.com',
            password='password',
            first_name=nickname,
            last_name='Test',
            nickname=nickname,
        )

    def _chat(self, *users):
        chat = Chat.objects.create()
        for user in users:
            ChatParticipant.objects.create(chat=chat, user=user)
        return chat

    def _forward(self, source_chat, message_ids, target_chat=None, comment=''):
        return self.client.post(
            f'/api/chats/{target_chat or self.target_chat.id}/messages/forward/',
            {
                'source_chat_id': str(source_chat.id),
                'message_ids': [str(message_id) for message_id in message_ids],
                'comment': comment,
            },
            format='json',
        )

    @patch('apps.notifications.tasks.enqueue_message_push')
    def test_single_forward_creates_snapshot_and_sets_origin(self, push_delay):
        source = Message.objects.create(
            chat=self.source_chat,
            sender=self.source_peer,
            content='original text',
        )

        response = self._forward(self.source_chat, [source.id], comment='Look')

        self.assertEqual(response.status_code, 201)
        forwarded = Message.objects.get(id=response.data['id'])
        self.assertEqual(forwarded.message_type, MessageType.FORWARD)
        self.assertEqual(forwarded.content, 'Look')
        self.assertEqual(forwarded.forwarded_from_id, source.id)
        self.assertEqual(forwarded.forward_bundle[0]['original_id'], str(source.id))
        self.assertEqual(
            forwarded.forward_bundle[0]['original_chat_id'],
            str(self.source_chat.id),
        )
        self.assertEqual(forwarded.forward_bundle[0]['sender']['nickname'], 'source_peer')
        self.assertNotIn('content_url', forwarded.forward_bundle[0])
        push_delay.assert_called_once_with(
            str(forwarded.id),
            str(self.target_peer.id),
        )

    @patch('apps.notifications.tasks.enqueue_message_push')
    def test_multi_forward_preserves_requested_order(self, _push_delay):
        first = Message.objects.create(
            chat=self.source_chat,
            sender=self.user,
            content='first',
        )
        second = Message.objects.create(
            chat=self.source_chat,
            sender=self.source_peer,
            content='second',
        )

        response = self._forward(self.source_chat, [second.id, first.id])

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['content'], '')
        self.assertEqual(
            [item['original_id'] for item in response.data['forward_bundle']],
            [str(second.id), str(first.id)],
        )

    @patch('apps.notifications.tasks.enqueue_message_push')
    @patch('apps.chats.forward_services.upload_file')
    @patch('apps.chats.forward_services.download_object_bytes', return_value=b'image')
    def test_photo_paths_are_copied_and_urls_are_not_persisted(
        self,
        download,
        upload,
        _push_delay,
    ):
        source_path = f'chat-files/{self.source_chat.id}/photo.jpg'
        upload.side_effect = (
            lambda bucket, object_name, file_data, content_type:
            f'{bucket}/{object_name}'
        )
        source = Message.objects.create(
            chat=self.source_chat,
            sender=self.source_peer,
            message_type=MessageType.PHOTO,
            content=source_path,
            file_name='photo.jpg',
            mime_type='image/jpeg',
            file_size=5,
            attachments=[{
                'path': source_path,
                'file_name': 'photo.jpg',
                'mime_type': 'image/jpeg',
                'file_size': 5,
            }],
        )

        with patch(
            'apps.chats.serializers.get_presigned_url',
            side_effect=lambda path: f'https://media.test/{path}',
        ):
            response = self._forward(self.source_chat, [source.id])

        self.assertEqual(response.status_code, 201)
        forwarded = Message.objects.get(id=response.data['id'])
        item = forwarded.forward_bundle[0]
        self.assertNotEqual(item['content'], source_path)
        self.assertTrue(
            item['content'].startswith(f'chat-files/{self.target_chat.id}/forwards/')
        )
        self.assertEqual(item['attachments'][0]['path'], item['content'])
        self.assertNotIn('content_url', item)
        self.assertNotIn('content_url', item['attachments'][0])
        self.assertEqual(download.call_count, 1)
        self.assertEqual(
            response.data['forward_bundle'][0]['content_url'],
            f"https://media.test/{item['content']}",
        )
        with patch('apps.chats.services.delete_object') as delete:
            delete_message_for_user(forwarded, self.user, 'everyone')
        delete.assert_called_once_with(item['content'])

    @patch('apps.notifications.tasks.enqueue_message_push')
    def test_requires_access_to_source_and_target(self, _push_delay):
        inaccessible_source = Message.objects.create(
            chat=self.outsider_chat,
            sender=self.outsider,
            content='private',
        )
        source_response = self._forward(
            self.outsider_chat,
            [inaccessible_source.id],
        )
        target_response = self._forward(
            self.source_chat,
            [Message.objects.create(
                chat=self.source_chat,
                sender=self.source_peer,
                content='visible',
            ).id],
            target_chat=self.outsider_chat.id,
        )

        self.assertEqual(source_response.status_code, 403)
        self.assertEqual(target_response.status_code, 403)
        self.assertFalse(
            Message.objects.filter(message_type=MessageType.FORWARD).exists()
        )

    @patch('apps.notifications.tasks.enqueue_message_push')
    def test_rejects_call_messages_strictly(self, _push_delay):
        text = Message.objects.create(
            chat=self.source_chat,
            sender=self.source_peer,
            content='valid',
        )
        call = Message.objects.create(
            chat=self.source_chat,
            sender=self.source_peer,
            message_type=MessageType.CALL,
            content='Звонок завершён',
        )

        response = self._forward(self.source_chat, [text.id, call.id])

        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            Message.objects.filter(message_type=MessageType.FORWARD).exists()
        )


@override_settings(CACHES=TEST_CACHES, CHANNEL_LAYERS=TEST_CHANNEL_LAYERS)
class ReplyConsumerTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='reply@example.com',
            password='password',
            first_name='Reply',
            last_name='User',
            nickname='reply_user',
        )
        self.peer = User.objects.create_user(
            email='reply-peer@example.com',
            password='password',
            first_name='Reply',
            last_name='Peer',
            nickname='reply_peer',
        )
        self.chat = Chat.objects.create()
        ChatParticipant.objects.create(chat=self.chat, user=self.user)
        ChatParticipant.objects.create(chat=self.chat, user=self.peer)
        self.other_chat = Chat.objects.create()
        ChatParticipant.objects.create(chat=self.other_chat, user=self.user)
        ChatParticipant.objects.create(chat=self.other_chat, user=self.peer)
        self.reply = Message.objects.create(
            chat=self.chat,
            sender=self.peer,
            content='reply target',
        )
        self.other_reply = Message.objects.create(
            chat=self.other_chat,
            sender=self.peer,
            content='wrong chat',
        )
        self.application = URLRouter([
            re_path(
                r'ws/chat/(?P<chat_id>[0-9a-f-]+)/$',
                ChatConsumer.as_asgi(),
            ),
        ])

    @patch('apps.notifications.tasks.enqueue_message_push')
    def test_reply_must_be_visible_and_in_same_chat(self, _push_delay):
        async_to_sync(self._reply_scenario)()

    async def _reply_scenario(self):
        communicator = WebsocketCommunicator(
            self.application,
            f'/ws/chat/{self.chat.id}/',
        )
        communicator.scope['user'] = self.user
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        await communicator.send_json_to({
            'action': 'message.send',
            'content': 'valid reply',
            'message_type': 'text',
            'reply_to': str(self.reply.id),
        })
        event = await communicator.receive_json_from()
        self.assertEqual(
            event['message']['reply_to_summary']['id'],
            str(self.reply.id),
        )
        self.assertEqual(
            event['message']['reply_to_summary']['sender']['nickname'],
            self.peer.nickname,
        )

        await communicator.send_json_to({
            'action': 'message.send',
            'content': 'invalid reply',
            'message_type': 'text',
            'reply_to': str(self.other_reply.id),
        })
        self.assertTrue(await communicator.receive_nothing(timeout=0.1))
        self.assertFalse(
            await Message.objects.filter(
                chat=self.chat,
                content='invalid reply',
            ).aexists()
        )
        await communicator.disconnect()
