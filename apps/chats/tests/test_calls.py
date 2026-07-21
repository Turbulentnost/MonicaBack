import uuid
from unittest.mock import patch

from asgiref.sync import async_to_sync
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.core.cache import cache
from django.test import TransactionTestCase, override_settings
from django.urls import re_path
from rest_framework.test import APIClient

from apps.chats.call_services import (
    CallError,
    accept_call,
    busy_key,
    expire_ringing_call,
    hangup_call,
    start_call,
)
from apps.chats.consumers_call import CallSignalingConsumer
from apps.chats.models import CallSession, CallStatus, Chat, ChatParticipant
from apps.users.models import User


TEST_CHANNEL_LAYERS = {
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'},
}
TEST_CACHES = {
    'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'},
}


@override_settings(CACHES=TEST_CACHES, CHANNEL_LAYERS=TEST_CHANNEL_LAYERS)
class CallServiceAndEndpointTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.caller = User.objects.create_user(
            email='caller@example.com',
            password='password',
            first_name='Caller',
            last_name='One',
            nickname='caller_one',
        )
        self.callee = User.objects.create_user(
            email='callee@example.com',
            password='password',
            first_name='Callee',
            last_name='Two',
            nickname='callee_two',
        )
        self.outsider = User.objects.create_user(
            email='other@example.com',
            password='password',
            first_name='Other',
            last_name='Three',
            nickname='other_three',
        )
        self.chat = Chat.objects.create()
        ChatParticipant.objects.create(chat=self.chat, user=self.caller)
        ChatParticipant.objects.create(chat=self.chat, user=self.callee)

    @patch('apps.chats.call_services.is_user_online', return_value=False)
    def test_start_rejects_offline_callee(self, _online):
        with self.assertRaises(CallError) as raised:
            start_call(self.chat.id, self.caller, uuid.uuid4())
        self.assertEqual(raised.exception.code, 'offline')
        self.assertEqual(raised.exception.http_status, 409)

    @patch('apps.chats.call_services.broadcast_call_ended')
    @patch('apps.chats.call_services.broadcast_user_event')
    @patch('apps.chats.call_services._schedule_expiry')
    @patch('apps.chats.call_services.is_user_online', return_value=True)
    def test_call_state_roles_and_idempotency(
        self, _online, _schedule, _broadcast, _ended
    ):
        instance_id = uuid.uuid4()
        call, created = start_call(self.chat.id, self.caller, instance_id)
        duplicate, duplicate_created = start_call(
            self.chat.id, self.caller, instance_id
        )
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate.id, call.id)

        with self.assertRaises(CallError) as raised:
            accept_call(call.id, self.caller)
        self.assertEqual(raised.exception.code, 'forbidden')

        winning_instance_id = uuid.uuid4()
        accepted, changed = accept_call(call.id, self.callee, winning_instance_id)
        self.assertTrue(changed)
        self.assertEqual(accepted.status, CallStatus.ACTIVE)
        duplicate_accept, changed = accept_call(
            call.id, self.callee, uuid.uuid4()
        )
        self.assertFalse(changed)
        self.assertEqual(
            duplicate_accept.accepted_client_instance_id,
            winning_instance_id,
        )

        ended, changed = hangup_call(call.id, self.caller)
        self.assertTrue(changed)
        self.assertEqual(ended.status, CallStatus.ENDED)

        repeated, repeated_created = start_call(
            self.chat.id, self.caller, instance_id
        )
        self.assertTrue(repeated_created)
        self.assertNotEqual(repeated.id, call.id)

    @patch('apps.chats.call_services.broadcast_user_event')
    @patch('apps.chats.call_services._schedule_expiry')
    @patch('apps.chats.call_services.is_user_online', return_value=True)
    def test_start_and_active_endpoints(self, _online, _schedule, _broadcast):
        client = APIClient()
        client.force_authenticate(self.caller)
        response = client.post(
            f'/api/chats/{self.chat.id}/calls/start/',
            {'client_instance_id': str(uuid.uuid4())},
            format='json',
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], CallStatus.RINGING)
        self.assertEqual(response.data['caller']['id'], str(self.caller.id))

        active = client.get('/api/calls/active/')
        self.assertEqual(active.status_code, 200)
        self.assertEqual(active.data['call']['id'], response.data['id'])

    @patch('apps.chats.call_services.broadcast_call_ended')
    @patch('apps.chats.call_services.broadcast_user_event')
    @patch('apps.chats.call_services._schedule_expiry')
    @patch('apps.chats.call_services.is_user_online', return_value=True)
    def test_timeout_marks_missed_and_releases_busy_locks(
        self, _online, _schedule, _broadcast, _ended
    ):
        call, _ = start_call(self.chat.id, self.caller, uuid.uuid4())
        expired, changed = expire_ringing_call(call.id)
        self.assertTrue(changed)
        self.assertEqual(expired.status, CallStatus.MISSED)
        self.assertIsNone(cache.get(busy_key(self.caller.id)))
        self.assertIsNone(cache.get(busy_key(self.callee.id)))


@override_settings(CACHES=TEST_CACHES, CHANNEL_LAYERS=TEST_CHANNEL_LAYERS)
class CallConsumerTests(TransactionTestCase):
    def setUp(self):
        self.caller = User.objects.create_user(
            email='ws-caller@example.com',
            password='password',
            first_name='Caller',
            last_name='Ws',
            nickname='ws_caller',
        )
        self.callee = User.objects.create_user(
            email='ws-callee@example.com',
            password='password',
            first_name='Callee',
            last_name='Ws',
            nickname='ws_callee',
        )
        self.outsider = User.objects.create_user(
            email='ws-other@example.com',
            password='password',
            first_name='Other',
            last_name='Ws',
            nickname='ws_other',
        )
        chat = Chat.objects.create()
        ChatParticipant.objects.create(chat=chat, user=self.caller)
        ChatParticipant.objects.create(chat=chat, user=self.callee)
        self.call = CallSession.objects.create(
            chat=chat,
            caller=self.caller,
            callee=self.callee,
            client_instance_id=uuid.uuid4(),
        )
        self.application = URLRouter([
            re_path(
                r'ws/call/(?P<call_id>[0-9a-f-]+)/$',
                CallSignalingConsumer.as_asgi(),
            ),
        ])

    def test_consumer_permissions_and_offer_role(self):
        async_to_sync(self._consumer_permissions_scenario)()

    def test_signaling_is_relayed_only_to_other_participant(self):
        self.call.status = CallStatus.ACTIVE
        self.call.save(update_fields=['status'])
        async_to_sync(self._targeted_relay_scenario)()

    async def _consumer_permissions_scenario(self):
        outsider = WebsocketCommunicator(
            self.application,
            f'/ws/call/{self.call.id}/',
        )
        outsider.scope['user'] = self.outsider
        connected, close_code = await outsider.connect()
        self.assertFalse(connected)
        self.assertEqual(close_code, 4003)

        callee = WebsocketCommunicator(
            self.application,
            f'/ws/call/{self.call.id}/',
        )
        callee.scope['user'] = self.callee
        connected, _ = await callee.connect()
        self.assertTrue(connected)
        ready = await callee.receive_json_from()
        self.assertEqual(ready['action'], 'call.ready')

        await callee.send_json_to({
            'action': 'call.offer',
            'data': {'type': 'offer', 'sdp': 'test'},
        })
        error = await callee.receive_json_from()
        self.assertEqual(error['action'], 'call.error')
        self.assertEqual(error['code'], 'forbidden')
        await callee.disconnect()

    async def _targeted_relay_scenario(self):
        caller = WebsocketCommunicator(
            self.application,
            f'/ws/call/{self.call.id}/',
        )
        caller.scope['user'] = self.caller
        callee = WebsocketCommunicator(
            self.application,
            f'/ws/call/{self.call.id}/',
        )
        callee.scope['user'] = self.callee
        other_caller_tab = WebsocketCommunicator(
            self.application,
            f'/ws/call/{self.call.id}/',
        )
        other_caller_tab.scope['user'] = self.caller

        for communicator in (caller, callee, other_caller_tab):
            connected, _ = await communicator.connect()
            self.assertTrue(connected)
            await communicator.receive_json_from()

        await caller.send_json_to({
            'action': 'call.offer',
            'data': {'sdp': {'type': 'offer', 'sdp': 'test'}},
        })
        relayed = await callee.receive_json_from()
        self.assertEqual(relayed['action'], 'call.offer')
        self.assertEqual(relayed['from_user_id'], str(self.caller.id))
        self.assertTrue(await other_caller_tab.receive_nothing(timeout=0.1))

        await caller.disconnect()
        await callee.disconnect()
        await other_caller_tab.disconnect()
