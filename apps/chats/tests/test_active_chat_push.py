from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from apps.chats.presence import (
    is_user_viewing_chat,
    mark_chat_viewing,
    touch_chat_viewing,
    unmark_chat_viewing,
)


TEST_CACHES = {
    'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'},
}


@override_settings(CACHES=TEST_CACHES)
class ActiveChatViewingTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_viewing_refcount_and_push_gate(self):
        self.assertFalse(is_user_viewing_chat(1, 'chat-a'))
        mark_chat_viewing(1, 'chat-a')
        mark_chat_viewing(1, 'chat-a')
        self.assertTrue(is_user_viewing_chat(1, 'chat-a'))
        self.assertFalse(is_user_viewing_chat(1, 'chat-b'))

        unmark_chat_viewing(1, 'chat-a')
        self.assertTrue(is_user_viewing_chat(1, 'chat-a'))
        unmark_chat_viewing(1, 'chat-a')
        self.assertFalse(is_user_viewing_chat(1, 'chat-a'))

    def test_touch_keeps_viewing_alive(self):
        mark_chat_viewing(7, 'chat-x')
        self.assertTrue(touch_chat_viewing(7, 'chat-x'))
        self.assertTrue(is_user_viewing_chat(7, 'chat-x'))
        self.assertFalse(touch_chat_viewing(7, 'missing'))
