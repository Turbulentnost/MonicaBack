from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('chats', '0004_message_read_at'),
    ]

    operations = [
        migrations.AlterField(
            model_name='message',
            name='message_type',
            field=models.CharField(
                choices=[
                    ('text', 'Текст'),
                    ('photo', 'Фото'),
                    ('file', 'Файл'),
                    ('voice', 'Голосовое сообщение'),
                    ('code', 'Код'),
                    ('forward', 'Пересылка'),
                ],
                default='text',
                max_length=10,
            ),
        ),
    ]
