import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('chats', '0008_message_edited_at'),
    ]

    operations = [
        migrations.CreateModel(
            name='CallSession',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('status', models.CharField(choices=[('ringing', 'Вызов'), ('active', 'Активен'), ('rejected', 'Отклонён'), ('cancelled', 'Отменён'), ('missed', 'Пропущен'), ('ended', 'Завершён'), ('failed', 'Ошибка')], default='ringing', max_length=16)),
                ('client_instance_id', models.UUIDField()),
                ('accepted_client_instance_id', models.UUIDField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('accepted_at', models.DateTimeField(blank=True, null=True)),
                ('connected_at', models.DateTimeField(blank=True, null=True)),
                ('ended_at', models.DateTimeField(blank=True, null=True)),
                ('end_reason', models.CharField(blank=True, default='', max_length=64)),
                ('callee', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='calls_received', to=settings.AUTH_USER_MODEL)),
                ('caller', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='calls_started', to=settings.AUTH_USER_MODEL)),
                ('chat', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='calls', to='chats.chat')),
                ('ended_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='calls_ended', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['caller', 'status'], name='chats_calls_caller__5a4035_idx'),
                    models.Index(fields=['callee', 'status'], name='chats_calls_callee__22be8d_idx'),
                ],
                'constraints': [
                    models.UniqueConstraint(
                        condition=models.Q(status__in=['ringing', 'active']),
                        fields=('caller', 'client_instance_id'),
                        name='unique_active_call_client_instance',
                    ),
                ],
            },
        ),
    ]
