import io

from django.conf import settings
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
    return f'{bucket}/{object_name}'


def get_presigned_url(object_path, expires_hours=24):
    if not object_path:
        return None
    parts = object_path.split('/', 1)
    if len(parts) != 2:
        return None
    bucket, object_name = parts
    from datetime import timedelta
    client = get_minio_client()
    try:
        return client.presigned_get_object(
            bucket, object_name, expires=timedelta(hours=expires_hours)
        )
    except S3Error:
        return None
