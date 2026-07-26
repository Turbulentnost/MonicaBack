import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('chats', '0013_chatparticipant_background'),
    ]

    operations = [
        migrations.AddField(
            model_name='chat',
            name='chat_type',
            field=models.CharField(
                choices=[('direct', 'Личный'), ('group', 'Группа')],
                db_index=True,
                default='direct',
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name='chat',
            name='created_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='chats_created',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name='chat',
            name='title',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='chatparticipant',
            name='role',
            field=models.CharField(
                choices=[
                    ('owner', 'Владелец'),
                    ('admin', 'Админ'),
                    ('member', 'Участник'),
                ],
                default='member',
                max_length=16,
            ),
        ),
    ]
