from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chats', '0006_message_voice_waveform'),
    ]

    operations = [
        migrations.AddField(
            model_name='message',
            name='attachments',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
