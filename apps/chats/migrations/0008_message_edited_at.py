from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chats', '0007_message_attachments'),
    ]

    operations = [
        migrations.AddField(
            model_name='message',
            name='edited_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
