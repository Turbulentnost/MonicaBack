import django.db.models.deletion
import pgvector.django.indexes
import pgvector.django.vector
from django.conf import settings
from django.db import migrations, models
from pgvector.django import VectorExtension


class Migration(migrations.Migration):

    dependencies = [
        ('ai', '0003_partnerstyleprofile_messages_since_refresh'),
        ('chats', '0017_message_pin'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        VectorExtension(),
        migrations.CreateModel(
            name='ChatTopicSegment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('started_at', models.DateTimeField()),
                ('ended_at', models.DateTimeField(blank=True, null=True)),
                ('label', models.CharField(blank=True, default='', max_length=160)),
                ('centroid', pgvector.django.vector.VectorField(blank=True, dimensions=1024, null=True)),
                ('message_count', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('anchor_message', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='chats.message')),
                ('chat', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='topic_segments', to='chats.chat')),
            ],
            options={
                'verbose_name': 'Chat topic segment',
                'verbose_name_plural': 'Chat topic segments',
            },
        ),
        migrations.CreateModel(
            name='MessageEmbedding',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('embedding', pgvector.django.vector.VectorField(dimensions=1024)),
                ('content_hash', models.CharField(blank=True, default='', max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('chat', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='message_embeddings', to='chats.chat')),
                ('message', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='embedding', to='chats.message')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='message_embeddings', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Message embedding',
                'verbose_name_plural': 'Message embeddings',
            },
        ),
        migrations.AddIndex(
            model_name='chattopicsegment',
            index=models.Index(fields=['chat', 'ended_at'], name='ai_topic_seg_chat_ended'),
        ),
        migrations.AddIndex(
            model_name='chattopicsegment',
            index=models.Index(fields=['chat', '-started_at'], name='ai_topic_seg_chat_started'),
        ),
        migrations.AddIndex(
            model_name='messageembedding',
            index=pgvector.django.indexes.HnswIndex(ef_construction=64, fields=['embedding'], m=16, name='ai_msg_emb_hnsw_cosine', opclasses=['vector_cosine_ops']),
        ),
        migrations.AddIndex(
            model_name='messageembedding',
            index=models.Index(fields=['chat', 'created_at'], name='ai_msg_emb_chat_created'),
        ),
    ]
