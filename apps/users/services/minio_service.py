import io
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from minio import Minio
from minio.error import S3Error


def get_minio_client():
    return Minio(
        settings.MINIO_ENDPOINT,
        access_key=settings.MINIO_ACCESS_KEY,
        secret_key=settings.MINIO_SECRET_KEY,
        secure=settings.MINIO_USE_SSL,
    )


def ensure_buckets():
    client = get_minio_client()
    for bucket in (settings.MINIO_BUCKET_AVATARS, settings.MINIO_BUCKET_CHAT_FILES):
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)


def _presigned_cache_key(object_path):
    return f'presigned_url:{object_path}'


def invalidate_presigned_url(object_path):
    if object_path:
        cache.delete(_presigned_cache_key(object_path))


def upload_file(bucket, object_name, file_data, content_type):
    client = get_minio_client()
    file_data.seek(0)
    size = getattr(file_data, 'size', None)
    if size is None:
        data = file_data.read()
        size = len(data)
        file_data = io.BytesIO(data)
    client.put_object(
        bucket,
        object_name,
        file_data,
        length=size,
        content_type=content_type,
    )
    path = f'{bucket}/{object_name}'
    invalidate_presigned_url(path)
    return path


def get_presigned_url(object_path, expires_hours=24):
    if not object_path:
        return None
    parts = object_path.split('/', 1)
    if len(parts) != 2:
        return None

    cache_key = _presigned_cache_key(object_path)
    cached = cache.get(cache_key)
    if cached:
        return cached

    bucket, object_name = parts
    client = get_minio_client()
    try:
        url = client.presigned_get_object(
            bucket, object_name, expires=timedelta(hours=expires_hours)
        )
    except S3Error:
        return None

    # Чуть меньше срока MinIO, чтобы не отдавать уже просроченный URL
    ttl = getattr(settings, 'PHOTO_URL_CACHE_TTL', max(3600, (expires_hours - 1) * 3600))
    cache.set(cache_key, url, ttl)
    return url


def delete_object(object_path):
    if not object_path:
        return False
    parts = object_path.split('/', 1)
    if len(parts) != 2:
        return False
    bucket, object_name = parts
    client = get_minio_client()
    try:
        client.remove_object(bucket, object_name)
    except S3Error:
        return False
    invalidate_presigned_url(object_path)
    return True


def download_object_bytes(object_path, max_bytes=None):
    """Скачать объект из MinIO в память. max_bytes — жёсткий потолок."""
    if not object_path:
        return None
    parts = object_path.split('/', 1)
    if len(parts) != 2:
        return None
    bucket, object_name = parts
    client = get_minio_client()
    try:
        response = client.get_object(bucket, object_name)
        try:
            if max_bytes is None:
                return response.read()
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError('Файл слишком большой для запуска')
            return data
        finally:
            response.close()
            response.release_conn()
    except S3Error:
        return None
