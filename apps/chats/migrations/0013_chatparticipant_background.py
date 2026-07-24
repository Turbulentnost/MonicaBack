from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chats', '0012_message_forward_bundle_message_reply_to'),
    ]

    operations = [
        migrations.AddField(
            model_name='chatparticipant',
            name='background',
            field=models.CharField(blank=True, default='', max_length=512),
        ),
    ]
