# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ai', '0002_partnerstyleprofile'),
    ]

    operations = [
        migrations.AddField(
            model_name='partnerstyleprofile',
            name='messages_since_refresh',
            field=models.PositiveIntegerField(default=0),
        ),
    ]
