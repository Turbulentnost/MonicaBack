#!/bin/sh
set -e

echo "Waiting for postgres..."
until python -c "import socket; s=socket.socket(); s.settimeout(2); s.connect(('${POSTGRES_HOST:-postgres}', int('${POSTGRES_PORT:-5432}'))); s.close()" 2>/dev/null; do
  sleep 1
done

echo "Running migrations..."
python manage.py migrate --noinput

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Ensuring MinIO buckets..."
python manage.py shell -c "from apps.users.services.minio_service import ensure_buckets; ensure_buckets()" || true

echo "Starting Daphne..."
exec daphne -b 0.0.0.0 -p 8000 config.asgi:application
