import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ai', '0001_initial'),
        ('chats', '0009_callsession'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='PartnerStyleProfile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('notes', models.TextField(blank=True, default='')),
                ('traits', models.JSONField(blank=True, default=dict)),
                ('last_day_key', models.CharField(blank=True, default='', max_length=10)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                (
                    'chat',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='partner_styles',
                        to='chats.chat',
                    ),
                ),
                (
                    'partner',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='partner_styles_about',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    'user',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='partner_styles',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                'verbose_name': 'Partner style profile',
                'verbose_name_plural': 'Partner style profiles',
            },
        ),
        migrations.AddConstraint(
            model_name='partnerstyleprofile',
            constraint=models.UniqueConstraint(
                fields=('user', 'partner'),
                name='ai_partnerstyle_user_partner_uniq',
            ),
        ),
    ]
