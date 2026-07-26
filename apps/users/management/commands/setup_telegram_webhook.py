from django.conf import settings
from django.core.management.base import BaseCommand

from apps.users.services.telegram_bot import (
    TelegramBotError,
    set_webhook,
    telegram_configured,
)


class Command(BaseCommand):
    help = 'Register Telegram bot webhook URL for registration OTP'

    def handle(self, *args, **options):
        if not telegram_configured():
            self.stdout.write(self.style.WARNING('TELEGRAM_BOT_TOKEN / USERNAME not set — skip'))
            return

        url = getattr(settings, 'TELEGRAM_WEBHOOK_URL', '') or ''
        if not url:
            self.stderr.write('TELEGRAM_WEBHOOK_URL is empty')
            return

        secret = getattr(settings, 'TELEGRAM_WEBHOOK_SECRET', '') or ''
        try:
            result = set_webhook(url, secret_token=secret)
        except TelegramBotError as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            return

        self.stdout.write(self.style.SUCCESS(f'Webhook set: {url}'))
        self.stdout.write(str(result))
