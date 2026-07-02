from django.core.management.base import BaseCommand

from apps.users.services.minio_service import ensure_buckets


class Command(BaseCommand):
    help = 'Create MinIO buckets for Monica messenger'

    def handle(self, *args, **options):
        ensure_buckets()
        self.stdout.write(self.style.SUCCESS('MinIO buckets created successfully'))
