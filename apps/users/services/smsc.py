"""SMSC.ru HTTP API client for OTP SMS."""

from __future__ import annotations

import logging
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings

logger = logging.getLogger(__name__)


class SmscError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def smsc_configured() -> bool:
    return bool(getattr(settings, 'SMSC_LOGIN', '') and getattr(settings, 'SMSC_PASSWORD', ''))


def send_sms(phone: str, message: str) -> dict:
    """
    Send SMS via SMSC.ru.
    phone: digits only international (7900...).
    Returns parsed JSON-ish dict from fmt=3.
    """
    if not smsc_configured():
        raise SmscError('SMSC не настроен (SMSC_LOGIN / SMSC_PASSWORD)')

    params = {
        'login': settings.SMSC_LOGIN,
        'psw': settings.SMSC_PASSWORD,
        'phones': phone,
        'mes': message,
        'fmt': 3,
        'charset': 'utf-8',
    }
    sender = getattr(settings, 'SMSC_SENDER', '') or ''
    if sender:
        params['sender'] = sender

    url = f'https://smsc.ru/sys/send.php?{urlencode(params)}'
    request = Request(url, method='GET')
    try:
        with urlopen(request, timeout=20) as response:
            body = response.read().decode('utf-8', errors='replace')
    except Exception as exc:
        logger.exception('SMSC request failed')
        raise SmscError(f'Ошибка связи с SMSC: {exc}') from exc

    # fmt=3 returns JSON: {"id":123,"cnt":1} or {"error":"...","error_code":N}
    import json

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SmscError(f'Некорректный ответ SMSC: {body[:200]}') from exc

    if isinstance(data, dict) and data.get('error_code'):
        raise SmscError(data.get('error') or 'Ошибка SMSC', code=data.get('error_code'))
    return data if isinstance(data, dict) else {'raw': data}
