import re

from rest_framework import serializers

# Digits only, E.164 without '+': 79001234567, 37499123456, ...
PHONE_RE = re.compile(r'^\d{10,15}$')


def normalize_phone(raw: str) -> str:
    """Normalize user input to digits-only international form."""
    if raw is None:
        raise serializers.ValidationError('Укажите номер телефона')
    text = str(raw).strip()
    if not text:
        raise serializers.ValidationError('Укажите номер телефона')

    # Keep leading + temporarily for RU "8..." handling.
    cleaned = re.sub(r'[^\d+]', '', text)
    if cleaned.startswith('+'):
        cleaned = cleaned[1:]
    cleaned = re.sub(r'\D', '', cleaned)

    # Russia local format 8XXXXXXXXXX → 7XXXXXXXXXX
    if len(cleaned) == 11 and cleaned.startswith('8'):
        cleaned = '7' + cleaned[1:]
    # Russia without country code: 9XXXXXXXXX
    if len(cleaned) == 10 and cleaned.startswith('9'):
        cleaned = '7' + cleaned

    if not PHONE_RE.match(cleaned):
        raise serializers.ValidationError(
            'Некорректный номер. Укажите в международном формате, например +79001234567'
        )
    return cleaned


def looks_like_phone(raw: str) -> bool:
    text = str(raw or '').strip()
    if not text:
        return False
    # Nickname can't start with + or be mostly digits with optional +.
    compact = re.sub(r'[\s\-()]', '', text)
    if compact.startswith('+'):
        return True
    digits = re.sub(r'\D', '', compact)
    return len(digits) >= 10 and len(digits) >= len(compact) * 0.8


def format_phone_display(phone: str) -> str:
    if not phone:
        return ''
    return f'+{phone}' if not str(phone).startswith('+') else str(phone)
