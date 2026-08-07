from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chats', '0017_message_pin'),
    ]

    operations = [
        migrations.AddField(
            model_name='chatparticipant',
            name='background_mobile',
            field=models.CharField(blank=True, default='', max_length=512),
        ),
        migrations.AddField(
            model_name='chatparticipant',
            name='background_mobile_updated_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
