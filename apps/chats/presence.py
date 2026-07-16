from django.core.cache import cache
from django.utils import timezone

CONN_KEY = 'presence:conn:{user_id}'
ALIVE_KEY = 'presence:alive:{user_id}'
ONLINE_SET_KEY = 'presence:online_ids'

# Пока WS жив, клиент шлёт ping ~каждые 20с. Если пингов нет — считаем offline.
ALIVE_TTL_SEC = 90
CONN_TTL_SEC = 90


def _conn_key(user_id):
    return CONN_KEY.format(user_id=user_id)


def _alive_key(user_id):
    return ALIVE_KEY.format(user_id=user_id)


def _touch_alive(uid: str):
    cache.set(_alive_key(uid), 1, timeout=ALIVE_TTL_SEC)


def _add_to_online_set(uid: str):
    online = set(cache.get(ONLINE_SET_KEY) or [])
    online.add(uid)
    cache.set(ONLINE_SET_KEY, list(online), timeout=None)


def _remove_from_online_set(uid: str):
    online = set(cache.get(ONLINE_SET_KEY) or [])
    online.discard(uid)
    cache.set(ONLINE_SET_KEY, list(online), timeout=None)


def record_last_seen(user_id):
    """Сохраняет момент выхода в БД. Возвращает ISO-строку или None."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    now = timezone.now()
    updated = User.objects.filter(id=user_id).update(last_seen_at=now)
    if not updated:
        return None
    return now.isoformat()


def get_last_seen_iso(user_id):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    value = User.objects.filter(id=user_id).values_list('last_seen_at', flat=True).first()
    return value.isoformat() if value else None


def is_user_online(user_id):
    """Online только если есть свежий heartbeat (не вечный счётчик)."""
    return bool(cache.get(_alive_key(str(user_id))))


def prune_stale_online():
    """Убирает «зомби» из списка online. Возвращает [(user_id, last_seen_at), ...]."""
    ids = [str(uid) for uid in (cache.get(ONLINE_SET_KEY) or [])]
    alive = []
    gone = []
    for uid in ids:
        if cache.get(_alive_key(uid)):
            alive.append(uid)
        else:
            cache.delete(_conn_key(uid))
            last_seen = record_last_seen(uid)
            gone.append((uid, last_seen))
    if gone:
        cache.set(ONLINE_SET_KEY, alive, timeout=None)
    return gone


def get_online_user_ids():
    prune_stale_online()
    return [str(uid) for uid in (cache.get(ONLINE_SET_KEY) or []) if cache.get(_alive_key(str(uid)))]


def mark_user_online(user_id):
    """Увеличивает счётчик вкладок. True — пользователь только что стал online."""
    uid = str(user_id)
    key = _conn_key(uid)
    count = (cache.get(key) or 0) + 1
    cache.set(key, count, timeout=CONN_TTL_SEC)
    _touch_alive(uid)
    if count == 1:
        _add_to_online_set(uid)
        return True
    _add_to_online_set(uid)
    return False


def mark_user_offline(user_id):
    """Уменьшает счётчик. True — пользователь полностью offline."""
    uid = str(user_id)
    key = _conn_key(uid)
    count = (cache.get(key) or 0) - 1
    if count <= 0:
        cache.delete(key)
        cache.delete(_alive_key(uid))
        _remove_from_online_set(uid)
        return True
    cache.set(key, count, timeout=CONN_TTL_SEC)
    _touch_alive(uid)
    return False


def heartbeat(user_id):
    """
    Обновляет TTL по ping. Если кэш протух, а WS ещё жив — восстанавливает online.
    Возвращает: 'ok' | 'restored' | 'ignored'
    """
    uid = str(user_id)
    key = _conn_key(uid)
    count = cache.get(key) or 0
    if count <= 0:
        cache.set(key, 1, timeout=CONN_TTL_SEC)
        _touch_alive(uid)
        was_listed = uid in {str(x) for x in (cache.get(ONLINE_SET_KEY) or [])}
        _add_to_online_set(uid)
        return 'ok' if was_listed else 'restored'

    cache.set(key, count, timeout=CONN_TTL_SEC)
    _touch_alive(uid)
    _add_to_online_set(uid)
    return 'ok'


def clear_all_presence(record_seen=False):
    """Сброс всех presence-ключей (после рестарта Daphne / ручная чистка)."""
    ids = [str(uid) for uid in (cache.get(ONLINE_SET_KEY) or [])]
    for uid in ids:
        if record_seen:
            try:
                record_last_seen(uid)
            except Exception:
                pass
        cache.delete(_conn_key(uid))
        cache.delete(_alive_key(uid))
    cache.delete(ONLINE_SET_KEY)
    try:
        client = cache.client.get_client(write=True)
        for pattern in (b'presence:conn:*', b'presence:alive:*'):
            for key in client.scan_iter(match=pattern, count=100):
                client.delete(key)
    except Exception:
        pass
