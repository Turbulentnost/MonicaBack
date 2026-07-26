from unittest.mock import patch

from asgiref.sync import async_to_sync
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase, override_settings
from django.urls import re_path
from rest_framework.test import APIClient

from apps.chats.consumers import ChatConsumer
from apps.chats.models import Chat, ChatType, Message
from apps.chats.services import get_or_create_direct_chat
from apps.users.models import User


TEST_CHANNEL_LAYERS = {
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'},
}
TEST_CACHES = {
    'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'},
}


@override_settings(CACHES=TEST_CACHES, CHANNEL_LAYERS=TEST_CHANNEL_LAYERS)
class GroupChatTests(TransactionTestCase):
    def setUp(self):
        self.owner = self._user('owner')
        self.alice = self._user('alice')
        self.bob = self._user('bob')
        self.carol = self._user('carol')
        self.outsider = self._user('outsider')
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def _user(self, nickname):
        return User.objects.create_user(
            email=f'{nickname}@example.com',
            password='password',
            first_name=nickname.title(),
            last_name='Test',
            nickname=nickname,
        )

    def _create_group(self, title='Team', member_ids=None, as_user=None):
        if as_user is not None:
            self.client.force_authenticate(as_user)
        return self.client.post(
            '/api/chats/groups/',
            {
                'title': title,
                'member_ids': [str(uid) for uid in (member_ids or [self.alice.id, self.bob.id])],
            },
            format='json',
        )

    def test_create_group_returns_list_shape(self):
        response = self._create_group('Dev Squad')

        self.assertEqual(response.status_code, 201)
        data = response.data
        self.assertEqual(data['chat_type'], 'group')
        self.assertTrue(data['is_group'])
        self.assertEqual(data['title'], 'Dev Squad')
        self.assertIsNone(data['partner'])
        self.assertEqual(data['members_count'], 3)
        self.assertEqual(len(data['members']), 3)

        owner_member = next(m for m in data['members'] if m['nickname'] == 'owner')
        self.assertEqual(owner_member['role'], 'owner')
        roles = {m['nickname']: m['role'] for m in data['members']}
        self.assertEqual(roles['alice'], 'member')
        self.assertEqual(roles['bob'], 'member')

        chat = Chat.objects.get(id=data['id'])
        self.assertEqual(chat.chat_type, ChatType.GROUP)
        self.assertEqual(chat.created_by_id, self.owner.id)

    def test_list_includes_group_and_keeps_direct(self):
        direct, _ = get_or_create_direct_chat(self.owner, self.carol)
        group_resp = self._create_group('Mixed')
        self.assertEqual(group_resp.status_code, 201)

        response = self.client.get('/api/chats/')
        self.assertEqual(response.status_code, 200)
        by_id = {item['id']: item for item in response.data}

        direct_item = by_id[direct.id]
        self.assertEqual(direct_item['chat_type'], 'direct')
        self.assertFalse(direct_item['is_group'])
        self.assertIsNone(direct_item['title'])
        self.assertIsNone(direct_item['members'])
        self.assertEqual(direct_item['members_count'], 2)
        self.assertEqual(direct_item['partner']['id'], str(self.carol.id))

        group_item = by_id[group_resp.data['id']]
        self.assertTrue(group_item['is_group'])
        self.assertEqual(group_item['title'], 'Mixed')
        self.assertIsNone(group_item['partner'])
        self.assertGreaterEqual(group_item['members_count'], 3)

    def test_create_group_validation(self):
        empty_title = self._create_group(title='   ')
        self.assertEqual(empty_title.status_code, 400)

        no_members = self.client.post(
            '/api/chats/groups/',
            {'title': 'Solo', 'member_ids': []},
            format='json',
        )
        self.assertEqual(no_members.status_code, 400)

        only_self = self.client.post(
            '/api/chats/groups/',
            {'title': 'Solo', 'member_ids': [str(self.owner.id)]},
            format='json',
        )
        self.assertEqual(only_self.status_code, 400)

    def test_outsider_cannot_read_group_messages(self):
        group_resp = self._create_group()
        chat_id = group_resp.data['id']
        Message.objects.create(
            chat_id=chat_id,
            sender=self.owner,
            content='secret group note',
        )

        self.client.force_authenticate(self.outsider)
        response = self.client.get(f'/api/chats/{chat_id}/messages/')
        self.assertEqual(response.status_code, 404)

        detail = self.client.get(f'/api/chats/{chat_id}/')
        self.assertEqual(detail.status_code, 404)

    def test_member_can_read_and_detail_matches_list(self):
        group_resp = self._create_group()
        chat_id = group_resp.data['id']
        Message.objects.create(
            chat_id=chat_id,
            sender=self.owner,
            content='hello group',
        )

        self.client.force_authenticate(self.alice)
        messages = self.client.get(f'/api/chats/{chat_id}/messages/')
        self.assertEqual(messages.status_code, 200)
        self.assertEqual(len(messages.data), 1)
        self.assertEqual(messages.data[0]['content'], 'hello group')

        detail = self.client.get(f'/api/chats/{chat_id}/')
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(detail.data['is_group'])
        self.assertEqual(detail.data['title'], 'Team')

    def test_add_and_remove_members(self):
        group_resp = self._create_group(member_ids=[self.alice.id])
        chat_id = group_resp.data['id']

        add_resp = self.client.post(
            f'/api/chats/{chat_id}/members/',
            {'user_ids': [str(self.bob.id), str(self.carol.id)]},
            format='json',
        )
        self.assertEqual(add_resp.status_code, 200)
        self.assertEqual(add_resp.data['members_count'], 4)

        # member cannot add
        self.client.force_authenticate(self.alice)
        forbidden = self.client.post(
            f'/api/chats/{chat_id}/members/',
            {'user_ids': [str(self.outsider.id)]},
            format='json',
        )
        self.assertEqual(forbidden.status_code, 403)

        # owner removes bob
        self.client.force_authenticate(self.owner)
        remove_resp = self.client.delete(f'/api/chats/{chat_id}/members/{self.bob.id}/')
        self.assertEqual(remove_resp.status_code, 200)
        self.assertEqual(remove_resp.data['members_count'], 3)

        # alice leaves
        self.client.force_authenticate(self.alice)
        leave = self.client.delete(f'/api/chats/{chat_id}/members/{self.alice.id}/')
        self.assertEqual(leave.status_code, 204)

    def test_cannot_remove_last_owner(self):
        group_resp = self._create_group(member_ids=[self.alice.id])
        chat_id = group_resp.data['id']

        response = self.client.delete(f'/api/chats/{chat_id}/members/{self.owner.id}/')
        self.assertEqual(response.status_code, 403)
        self.assertIn('владельца', response.data['detail'].lower())

    def test_patch_title(self):
        group_resp = self._create_group()
        chat_id = group_resp.data['id']

        response = self.client.patch(
            f'/api/chats/{chat_id}/',
            {'title': 'Renamed'},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['title'], 'Renamed')

        self.client.force_authenticate(self.alice)
        forbidden = self.client.patch(
            f'/api/chats/{chat_id}/',
            {'title': 'Hacked'},
            format='json',
        )
        self.assertEqual(forbidden.status_code, 403)

    def test_direct_chat_start_still_works_and_not_confused_with_group(self):
        # group containing owner+alice must not become their direct chat
        self._create_group(member_ids=[self.alice.id, self.bob.id])
        chat, created = get_or_create_direct_chat(self.owner, self.alice)
        self.assertTrue(created)
        self.assertEqual(chat.chat_type, ChatType.DIRECT)
        self.assertEqual(chat.participants.count(), 2)

        response = self.client.post(
            '/api/chats/start/',
            {'recipient_id': str(self.alice.id)},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['chat_type'], 'direct')
        self.assertFalse(response.data['is_group'])
        self.assertEqual(response.data['partner']['id'], str(self.alice.id))

    @patch('apps.notifications.tasks.enqueue_message_push')
    def test_group_ws_delivers_to_all_members(self, _push):
        group_resp = self._create_group(member_ids=[self.alice.id, self.bob.id])
        chat_id = str(group_resp.data['id'])

        application = URLRouter([
            re_path(r'ws/chat/(?P<chat_id>[0-9a-f-]+)/$', ChatConsumer.as_asgi()),
        ])

        async def _run():
            owner_ws = WebsocketCommunicator(application, f'/ws/chat/{chat_id}/')
            owner_ws.scope['user'] = self.owner
            alice_ws = WebsocketCommunicator(application, f'/ws/chat/{chat_id}/')
            alice_ws.scope['user'] = self.alice
            bob_ws = WebsocketCommunicator(application, f'/ws/chat/{chat_id}/')
            bob_ws.scope['user'] = self.bob
            outsider_ws = WebsocketCommunicator(application, f'/ws/chat/{chat_id}/')
            outsider_ws.scope['user'] = self.outsider

            self.assertTrue((await owner_ws.connect())[0])
            self.assertTrue((await alice_ws.connect())[0])
            self.assertTrue((await bob_ws.connect())[0])
            connected, _ = await outsider_ws.connect()
            self.assertFalse(connected)

            await owner_ws.send_json_to({
                'action': 'message.send',
                'message_type': 'text',
                'content': 'hi all',
            })
            for ws in (owner_ws, alice_ws, bob_ws):
                event = None
                for _ in range(5):
                    candidate = await ws.receive_json_from(timeout=2)
                    if candidate.get('action') == 'message.new':
                        event = candidate
                        break
                self.assertIsNotNone(event, 'не дождались message.new')
                self.assertEqual(event['message']['content'], 'hi all')

            await owner_ws.disconnect()
            await alice_ws.disconnect()
            await bob_ws.disconnect()

        async_to_sync(_run)()

    def test_group_call_rejected(self):
        group_resp = self._create_group(member_ids=[self.alice.id])
        chat_id = group_resp.data['id']
        response = self.client.post(
            f'/api/chats/{chat_id}/calls/start/',
            {'client_instance_id': '11111111-1111-1111-1111-111111111111'},
            format='json',
        )
        self.assertIn(response.status_code, (400, 409))
