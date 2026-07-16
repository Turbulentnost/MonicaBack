from django.apps import AppConfig


class ChatsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.chats'
    label = 'chats'

    def ready(self):
        # После рестарта Daphne старые WS уже мертвы, а счётчики в Redis
        # без disconnect остаются «online» навсегда — сбрасываем.
        try:
            from apps.chats.presence import clear_all_presence
            clear_all_presence()
        except Exception:
            pass
