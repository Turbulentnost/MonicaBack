"""Registration OTP delivery through Telegram bot deep links."""

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
    contact_share_keyboard,
    deep_link,
    remove_keyboard,
    send_message,
    telegram_configured,
)

logger = logging.getLogger(__name__)
User = get_user_model()


def _phone_code_key(phone: str) -> str:
    return f'phone_code:{phone}'


def _start_token_key(token: str) -> str:
    return f'tg_reg_start:{token}'


def _chat_pending_key(chat_id: int | str) -> str:
    return f'tg_reg_chat:{chat_id}'


def create_telegram_verification(phone: str) -> dict:
    """
    Create OTP + deep-link session. Code is sent only after the user opens
    the bot and confirms the phone via Telegram contact share.
    """
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
            f'Откройте Telegram и подтвердите номер '
            f'{format_phone_display(phone)} — бот пришлёт код.'
        ),
    }


def _send_code_to_chat(chat_id: int | str, phone: str, code: str) -> None:
    text = (
        f'Код подтверждения Monica для {format_phone_display(phone)}:\n\n'
        f'{code}\n\n'
        f'Код действует 15 минут. Вернитесь на сайт и введите его.'
    )
    send_message(chat_id, text, reply_markup=remove_keyboard())


def handle_telegram_update(update: dict) -> None:
    message = update.get('message') or {}
    chat = message.get('chat') or {}
    chat_id = chat.get('id')
    if not chat_id:
        return

    text = (message.get('text') or '').strip()
    contact = message.get('contact')

    if text.startswith('/start'):
        parts = text.split(maxsplit=1)
        start_token = parts[1].strip() if len(parts) > 1 else ''
        _handle_start(chat_id, start_token)
        return

    if contact:
        _handle_contact(chat_id, contact, message.get('from') or {})
        return


def _handle_start(chat_id: int | str, start_token: str) -> None:
    if not start_token:
        send_message(
            chat_id,
            'Чтобы получить код, начните регистрацию на сайте Monica '
            'и нажмите «Перейти в Telegram».',
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
    cache.set(
        _chat_pending_key(chat_id),
        json.dumps({'phone': phone, 'code': data['code'], 'start_token': start_token}),
        settings.REGISTRATION_CODE_TTL,
    )

    send_message(
        chat_id,
        (
            f'Регистрация в Monica\n\n'
            f'Номер с сайта: {format_phone_display(phone)}\n\n'
            f'Нажмите кнопку ниже, чтобы подтвердить, что это ваш номер в Telegram. '
            f'После этого бот сразу пришлёт код.'
        ),
        reply_markup=contact_share_keyboard(),
    )


def _handle_contact(chat_id: int | str, contact: dict, from_user: dict) -> None:
    pending_raw = cache.get(_chat_pending_key(chat_id))
    if not pending_raw:
        send_message(
            chat_id,
            'Сессия не найдена. Вернитесь на сайт Monica и снова нажмите «Перейти в Telegram».',
            reply_markup=remove_keyboard(),
        )
        return

    # Contact must belong to the user who pressed the button (anti-spoof).
    user_id = from_user.get('id')
    contact_user_id = contact.get('user_id')
    if user_id and contact_user_id and int(contact_user_id) != int(user_id):
        send_message(
            chat_id,
            'Нужно отправить именно свой номер (кнопка «Подтвердить номер телефона»).',
            reply_markup=contact_share_keyboard(),
        )
        return

    try:
        shared_phone = normalize_phone(contact.get('phone_number') or '')
    except serializers.ValidationError:
        shared_phone = ''

    pending = json.loads(pending_raw)
    expected = pending['phone']

    if not shared_phone or shared_phone != expected:
        send_message(
            chat_id,
            (
                f'Номер в Telegram ({format_phone_display(shared_phone) or "не распознан"}) '
                f'не совпадает с номером с сайта ({format_phone_display(expected)}).\n\n'
                f'Вернитесь на сайт и укажите тот же номер, что привязан к Telegram, '
                f'либо используйте другой аккаунт Telegram.'
            ),
            reply_markup=remove_keyboard(),
        )
        return

    code = pending['code']
    # Keep OTP in phone_code cache; drop one-time start/chat bindings.
    start_token = pending.get('start_token')
    if start_token:
        cache.delete(_start_token_key(start_token))
    cache.delete(_chat_pending_key(chat_id))

    try:
        _send_code_to_chat(chat_id, expected, code)
    except TelegramBotError:
        logger.exception('Failed to send OTP to chat %s', chat_id)
        send_message(
            chat_id,
            'Не удалось отправить код. Попробуйте ещё раз с сайта.',
            reply_markup=remove_keyboard(),
        )
