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


def send_fcm_to_tokens(*, tokens, title, body, data=None, channel_id='messages_monica', data_only=False):
    """Отправка multicast FCM. Без токенов — no-op.

    data_only=True — только data (для входящих звонков, чтобы onMessageReceived
    всегда отработал и показал full-screen notification на Android).
    """
    if not tokens:
        return {'sent': 0, 'failed': 0}

    _get_firebase_app()
    from firebase_admin import messaging

    data = {str(k): str(v) for k, v in (data or {}).items()}
    if title and 'title' not in data:
        data['title'] = str(title)
    if body and 'body' not in data:
        data['body'] = str(body)

    android_notification = None
    notification = None
    if not data_only:
        notification = messaging.Notification(title=title, body=body)
        # icon/color применяются, когда Android сам рисует notification-payload
        # (приложение в фоне). Имя icon = drawable без расширения.
        android_notification = messaging.AndroidNotification(
            channel_id=channel_id,
            icon='ic_stat_monica',
            color='#5B7FFF',
            default_vibrate_timings=True,
            default_light_settings=True,
            priority='high',
            visibility='private',
        )

    message = messaging.MulticastMessage(
        notification=notification,
        data=data,
        tokens=list(tokens),
        android=messaging.AndroidConfig(
            priority='high',
            notification=android_notification,
        ),
    )
    response = messaging.send_each_for_multicast(message)
    logger.info('FCM multicast: success=%s failure=%s', response.success_count, response.failure_count)
    return {'sent': response.success_count, 'failed': response.failure_count}
