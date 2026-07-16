from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('chats', '0005_message_voice_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='message',
            name='waveform',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='message',
            name='voice_duration_ms',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
