from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('chats', '0016_chat_favorites_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='message',
            name='is_pinned',
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name='message',
            name='pinned_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='message',
            name='pinned_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='pinned_messages',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
