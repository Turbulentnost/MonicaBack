import logging
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

_firebase_app = None


def _get_firebase_app():
    global _firebase_app
    if _firebase_app is not None:
        return _firebase_app

    import firebase_admin
    from firebase_admin import credentials

    cred_path = Path(settings.FIREBASE_CREDENTIALS_PATH)
    if not cred_path.is_absolute():
        cred_path = settings.BASE_DIR / cred_path

    if not cred_path.exists():
        raise FileNotFoundError(f'Firebase credentials not found: {cred_path}')

    if not firebase_admin._apps:
        _firebase_app = firebase_admin.initialize_app(
            credentials.Certificate(str(cred_path))
        )
    else:
        _firebase_app = firebase_admin.get_app()
    return _firebase_app


def send_fcm_to_tokens(*, tokens, title, body, data=None):
    """Отправка multicast FCM. Без токенов — no-op."""
    if not tokens:
        return {'sent': 0, 'failed': 0}

    _get_firebase_app()
    from firebase_admin import messaging

    data = {str(k): str(v) for k, v in (data or {}).items()}
    message = messaging.MulticastMessage(
        notification=messaging.Notification(title=title, body=body),
        data=data,
        tokens=list(tokens),
        android=messaging.AndroidConfig(
            priority='high',
            notification=messaging.AndroidNotification(channel_id='messages'),
        ),
    )
    response = messaging.send_each_for_multicast(message)
    logger.info('FCM multicast: success=%s failure=%s', response.success_count, response.failure_count)
    return {'sent': response.success_count, 'failed': response.failure_count}
