"""Registration OTP delivery through Telegram bot deep links.

Flow:
1. Site creates a one-time start token bound to {phone, code}.
2. User opens https://t.me/<bot>?start=<token>
3. Bot receives /start <token>, loads that phone/code, sends the code.
"""

from __future__ import annotations

import json
import logging
import random
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework import serializers

from apps.users.services.phone import format_phone_display, normalize_phone
from apps.users.services.telegram_bot import (
    TelegramBotError,
    bot_username,
    deep_link,
    send_message,
    telegram_configured,
)

logger = logging.getLogger(__name__)
User = get_user_model()


def _phone_code_key(phone: str) -> str:
    return f'phone_code:{phone}'


def _start_token_key(token: str) -> str:
    return f'tg_reg_start:{token}'


def create_telegram_verification(phone: str) -> dict:
    """Create OTP + unique deep link bound to this phone number."""
    phone = normalize_phone(phone)
    if User.objects.filter(phone=phone).exists():
        raise serializers.ValidationError({'phone': 'Пользователь с таким номером уже существует'})

    if not telegram_configured():
        if not settings.DEBUG:
            raise serializers.ValidationError({
                'detail': 'Подтверждение через Telegram временно недоступно. Попробуйте позже.',
            })
        code = f'{random.randint(0, 999999):06d}'
        cache.set(_phone_code_key(phone), code, settings.REGISTRATION_CODE_TTL)
        return {
            'phone': phone,
            'debug_code': code,
            'telegram_url': '',
            'bot_username': '',
            'channel': 'debug',
        }

    code = f'{random.randint(0, 999999):06d}'
    start_token = secrets.token_hex(16)  # 32 chars, Telegram start-safe
    ttl = settings.REGISTRATION_CODE_TTL

    cache.set(_phone_code_key(phone), code, ttl)
    cache.set(
        _start_token_key(start_token),
        json.dumps({'phone': phone, 'code': code}),
        ttl,
    )

    return {
        'phone': phone,
        'telegram_url': deep_link(start_token),
        'bot_username': bot_username(),
        'channel': 'telegram',
        'detail': (
            f'Откройте Telegram по кнопке ниже — бот пришлёт код '
            f'для {format_phone_display(phone)}.'
        ),
    }


def handle_telegram_update(update: dict) -> None:
    message = update.get('message') or {}
    chat = message.get('chat') or {}
    chat_id = chat.get('id')
    if not chat_id:
        return

    text = (message.get('text') or '').strip()
    if not text.startswith('/start'):
        send_message(
            chat_id,
            'Чтобы получить код, начните регистрацию на сайте Monica '
            'и нажмите «Перейти в Telegram».',
        )
        return

    parts = text.split(maxsplit=1)
    start_token = parts[1].strip() if len(parts) > 1 else ''
    _handle_start(chat_id, start_token)


def _handle_start(chat_id: int | str, start_token: str) -> None:
    if not start_token:
        send_message(
            chat_id,
            'Чтобы получить код, начните регистрацию на сайте Monica '
            'и нажмите «Перейти в Telegram» — ссылка привязана к вашему номеру.',
        )
        return

    raw = cache.get(_start_token_key(start_token))
    if not raw:
        send_message(
            chat_id,
            'Ссылка устарела или уже использована. Вернитесь на сайт и запросите код снова.',
        )
        return

    data = json.loads(raw)
    phone = data['phone']
    code = data['code']

    # One-time: this deep link is bound to exactly one phone/code pair.
    cache.delete(_start_token_key(start_token))

    try:
        send_message(
            chat_id,
            (
                f'Вы перешли по ссылке регистрации Monica.\n'
                f'Код для номера {format_phone_display(phone)}:\n\n'
                f'{code}\n\n'
                f'Код действует 15 минут. Вернитесь на сайт и введите его.'
            ),
        )
    except TelegramBotError:
        logger.exception('Failed to send OTP to chat %s for phone %s', chat_id, phone)
        # Restore token so user can retry the same link once after a transient error.
        cache.set(
            _start_token_key(start_token),
            json.dumps({'phone': phone, 'code': code}),
            settings.REGISTRATION_CODE_TTL,
        )
        send_message(
            chat_id,
            'Не удалось отправить код. Откройте ссылку с сайта ещё раз.',
        )
