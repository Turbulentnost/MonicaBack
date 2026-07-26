from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chats', '0014_chat_group_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='chat',
            name='photo',
            field=models.CharField(blank=True, default='', max_length=512),
        ),
    ]
