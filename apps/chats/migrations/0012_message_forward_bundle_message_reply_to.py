from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('chats', '0011_message_call_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='message',
            name='forward_bundle',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='message',
            name='reply_to',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='replies',
                to='chats.message',
            ),
        ),
    ]
