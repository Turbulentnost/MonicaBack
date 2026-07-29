from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient


class LmStudioProxyApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_health_is_public(self):
        with patch('apps.lmstudio.views.health', return_value=(200, {'status': 'ok'})):
            response = self.client.get('/api/lmstudio/health/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'ok')

    def test_models_without_auth(self):
        with patch(
            'apps.lmstudio.views.list_models',
            return_value=(200, {'object': 'list', 'data': [{'id': 'qwen3-vl-8b-thinking'}]}),
        ):
            response = self.client.get('/api/lmstudio/v1/models/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['data'][0]['id'], 'qwen3-vl-8b-thinking')

    def test_chat_completions_without_auth_forces_model(self):
        payload = {
            'model': 'anything-else',
            'messages': [{'role': 'user', 'content': 'hi'}],
        }

        def _fake_upstream(method, path, payload=None, timeout=None):
            self.assertEqual(method, 'POST')
            self.assertEqual(path, '/chat/completions')
            self.assertEqual(payload.get('model'), 'qwen3-vl-8b-thinking')
            return 200, {
                'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}],
            }

        with patch('apps.lmstudio.proxy._request_json', side_effect=_fake_upstream):
            response = self.client.post(
                '/api/lmstudio/v1/chat/completions/',
                payload,
                format='json',
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['choices'][0]['message']['content'], 'ok')
