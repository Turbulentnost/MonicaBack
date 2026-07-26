"""Telegram Bot API helpers for registration OTP via deep link."""

from __future__ import annotations

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings

logger = logging.getLogger(__name__)


class TelegramBotError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def telegram_configured() -> bool:
    return bool(
        getattr(settings, 'TELEGRAM_BOT_TOKEN', '')
        and getattr(settings, 'TELEGRAM_BOT_USERNAME', '')
    )


def bot_username() -> str:
    return (getattr(settings, 'TELEGRAM_BOT_USERNAME', '') or '').lstrip('@')


def deep_link(start_token: str) -> str:
    return f'https://t.me/{bot_username()}?start={start_token}'


def _api(method: str, payload: dict | None = None) -> dict:
    token = settings.TELEGRAM_BOT_TOKEN
    if not token:
        raise TelegramBotError('TELEGRAM_BOT_TOKEN не задан')

    url = f'https://api.telegram.org/bot{token}/{method}'
    data = json.dumps(payload or {}).encode('utf-8')
    request = Request(
        url,
        data=data,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urlopen(request, timeout=20) as response:
            body = response.read().decode('utf-8', errors='replace')
    except HTTPError as exc:
        raw = exc.read().decode('utf-8', errors='replace') if exc.fp else str(exc)
        logger.warning('Telegram API HTTP error %s: %s', exc.code, raw[:300])
        raise TelegramBotError(f'Telegram API HTTP {exc.code}', code=exc.code) from exc
    except URLError as exc:
        logger.exception('Telegram API network error')
        raise TelegramBotError(f'Ошибка связи с Telegram: {exc}') from exc

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise TelegramBotError(f'Некорректный ответ Telegram: {body[:200]}') from exc

    if not data.get('ok'):
        desc = data.get('description') or 'Ошибка Telegram Bot API'
        raise TelegramBotError(desc, code=data.get('error_code'))
    return data.get('result') or {}


def send_message(chat_id: int | str, text: str, reply_markup: dict | None = None) -> dict:
    payload = {
        'chat_id': chat_id,
        'text': text,
        'disable_web_page_preview': True,
    }
    if reply_markup is not None:
        payload['reply_markup'] = reply_markup
    return _api('sendMessage', payload)


def set_webhook(webhook_url: str, secret_token: str = '') -> dict:
    payload = {
        'url': webhook_url,
        'allowed_updates': ['message'],
        'drop_pending_updates': True,
    }
    if secret_token:
        payload['secret_token'] = secret_token
    return _api('setWebhook', payload)


def delete_webhook() -> dict:
    return _api('deleteWebhook', {'drop_pending_updates': True})


