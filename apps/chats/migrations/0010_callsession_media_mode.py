from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chats', '0009_callsession'),
    ]

    operations = [
        migrations.AddField(
            model_name='callsession',
            name='media_mode',
            field=models.CharField(
                choices=[('audio', 'Аудио'), ('video', 'Видео')],
                default='audio',
                max_length=8,
            ),
        ),
    ]
