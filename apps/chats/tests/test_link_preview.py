from django.test import SimpleTestCase

from apps.chats.link_preview import validate_preview_url


class LinkPreviewValidationTests(SimpleTestCase):
    def test_rejects_private_and_local(self):
        self.assertIsNone(validate_preview_url('http://127.0.0.1/'))
        self.assertIsNone(validate_preview_url('http://localhost/x'))
        self.assertIsNone(validate_preview_url('http://192.168.0.1/'))
        self.assertIsNone(validate_preview_url('ftp://example.com/'))
        self.assertIsNone(validate_preview_url('https://user:pass@example.com/'))

    def test_accepts_public_https(self):
        url = validate_preview_url('https://music.yandex.ru/album/1')
        self.assertEqual(url, 'https://music.yandex.ru/album/1')
