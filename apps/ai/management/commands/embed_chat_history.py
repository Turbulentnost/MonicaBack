from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.ai.embeddings import embed_and_segment_message, message_embeddable
from apps.chats.models import Message, MessageType


class Command(BaseCommand):
    help = 'Backfill message embeddings and topic segments'

    def add_arguments(self, parser):
        parser.add_argument('--chat_id', type=str, default='', help='Limit to one chat UUID')
        parser.add_argument('--days', type=int, default=30, help='How many days back (0 = all)')
        parser.add_argument('--limit', type=int, default=0, help='Max messages to process (0 = all)')
        parser.add_argument(
            '--sync',
            action='store_true',
            help='Run inline instead of Celery delay',
        )

    def handle(self, *args, **options):
        chat_id = (options.get('chat_id') or '').strip()
        days = int(options.get('days') or 0)
        limit = int(options.get('limit') or 0)
        sync = bool(options.get('sync'))

        qs = (
            Message.objects
            .filter(message_type=MessageType.TEXT, deleted_at__isnull=True)
            .order_by('created_at')
        )
        if chat_id:
            qs = qs.filter(chat_id=chat_id)
        if days > 0:
            qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=days))
        if limit > 0:
            qs = qs[:limit]

        total = 0
        ok = 0
        for message in qs.iterator(chunk_size=100):
            if not message_embeddable(message):
                continue
            total += 1
            if sync:
                result = embed_and_segment_message(message.id)
                if result.get('ok'):
                    ok += 1
            else:
                from apps.ai.tasks import embed_and_segment_message as task
                task.delay(str(message.id))
                ok += 1

        mode = 'sync' if sync else 'queued'
        self.stdout.write(self.style.SUCCESS(
            f'embed_chat_history {mode}: processed={total} ok={ok}'
        ))
