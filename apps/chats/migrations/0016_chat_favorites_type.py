from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chats', '0015_chat_photo'),
    ]

    operations = [
        migrations.AlterField(
            model_name='chat',
            name='chat_type',
            field=models.CharField(
                choices=[
                    ('direct', 'Личный'),
                    ('group', 'Группа'),
                    ('favorites', 'Избранное'),
                ],
                db_index=True,
                default='direct',
                max_length=16,
            ),
        ),
    ]
