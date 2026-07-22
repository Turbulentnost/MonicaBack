from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models import Q
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.chats.code_runner import run_javascript_source, run_python_source
from apps.chats.models import Chat, Message, MessageType
from apps.chats.serializers import ForwardMessagesSerializer, MessageSerializer
from apps.chats.services import (
    delete_message_for_user,
    get_chat_history_cache_version,
    get_chat_partner,
    get_last_visible_message,
    get_or_create_direct_chat,
    get_user_chats,
    get_visible_messages,
    upload_chat_files,
)
from django.http import HttpResponse
from mimetypes import guess_type

from apps.users.serializers import UserSerializer
from apps.users.services.minio_service import download_object_bytes

User = get_user_model()


class StartChatView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        recipient_id = request.data.get('recipient_id')
        if not recipient_id:
            return Response({'recipient_id': 'Обязательное поле'}, status=400)
        try:
            recipient = User.objects.get(id=recipient_id)
        except User.DoesNotExist:
            return Response({'detail': 'Пользователь не найден'}, status=404)

        chat, _ = get_or_create_direct_chat(request.user, recipient)
        partner = get_chat_partner(chat, request.user)
        return Response({
            'id': chat.id,
            'partner': UserSerializer(partner, context={'request': request}).data,
        }, status=status.HTTP_200_OK)


class ChatListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        chats = get_user_chats(request.user).prefetch_related(
            'participants__user', 'messages__sender'
        )
        result = []
        for chat in chats:
            partner = get_chat_partner(chat, request.user)
            last_message = get_last_visible_message(chat, request.user)
            ctx = {'request': request}
            result.append({
                'id': chat.id,
                'partner': UserSerializer(partner, context=ctx).data if partner else None,
                'last_message': MessageSerializer(last_message, context=ctx).data if last_message else None,
                'updated_at': chat.updated_at,
            })
        return Response(result)


class ChatMessagesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, chat_id):
        try:
            chat = get_user_chats(request.user).get(id=chat_id)
        except Chat.DoesNotExist:
            return Response({'detail': 'Чат не найден'}, status=404)

        try:
            page_size = int(request.query_params.get('limit', 100))
        except (TypeError, ValueError):
            page_size = 100
        page_size = max(1, min(page_size, 200))

        query = (request.query_params.get('q') or '').strip()
        around_id = request.query_params.get('around')
        before_id = request.query_params.get('before')
        visible = get_visible_messages(chat, request.user)

        if query:
            qs = (
                visible.filter(Q(content__icontains=query) | Q(file_name__icontains=query))
                .order_by('-sent_at')
            )
            page = list(qs[:page_size])
            return Response(
                MessageSerializer(page, many=True, context={'request': request}).data
            )

        if around_id:
            pivot = visible.filter(id=around_id).values('sent_at').first()
            if not pivot:
                return Response({'detail': 'Сообщение не найдено'}, status=404)
            older = list(
                visible.filter(sent_at__lt=pivot['sent_at'])
                .order_by('-sent_at')[: page_size // 2]
            )
            newer_limit = max(1, page_size - len(older))
            newer = list(
                visible.filter(sent_at__gte=pivot['sent_at'])
                .order_by('sent_at')[:newer_limit]
            )
            if len(older) + len(newer) < page_size:
                extra = page_size - len(older) - len(newer)
                older = list(
                    visible.filter(sent_at__lt=pivot['sent_at'])
                    .order_by('-sent_at')[: len(older) + extra]
                )
            page = list(reversed(older)) + newer
            return Response(
                MessageSerializer(page, many=True, context={'request': request}).data
            )

        version = get_chat_history_cache_version(chat.id)
        cache_key = (
            f'chat-history:{chat.id}:{request.user.id}:{version}:'
            f'{before_id or "latest"}:{page_size}'
        )
        cached_page = cache.get(cache_key)
        if cached_page is not None:
            return Response(cached_page)

        # Последние N сообщений (хронологически), а не первые N с начала истории.
        qs = visible.order_by('-sent_at')
        if before_id:
            pivot = (
                visible.filter(id=before_id)
                .values_list('sent_at', flat=True)
                .first()
            )
            if pivot is not None:
                qs = qs.filter(sent_at__lt=pivot)

        page = list(qs[:page_size])
        page.reverse()
        payload = list(
            MessageSerializer(page, many=True, context={'request': request}).data
        )
        cache.set(cache_key, payload, settings.CHAT_HISTORY_CACHE_TTL)
        return Response(payload)


class ChatFilesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, chat_id):
        try:
            chat = get_user_chats(request.user).get(id=chat_id)
        except Chat.DoesNotExist:
            return Response({'detail': 'Чат не найден'}, status=404)

        messages = (
            get_visible_messages(chat, request.user)
            .filter(message_type__in=[MessageType.FILE, MessageType.PHOTO])
            .order_by('-sent_at')
        )
        return Response(
            MessageSerializer(messages, many=True, context={'request': request}).data
        )


class ChatMessageUploadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, chat_id):
        try:
            chat = get_user_chats(request.user).get(id=chat_id)
        except Chat.DoesNotExist:
            return Response({'detail': 'Чат не найден'}, status=404)

        uploaded_files = list(request.FILES.getlist('files'))
        if not uploaded_files:
            single = request.FILES.get('file')
            if single:
                uploaded_files = [single]
        if not uploaded_files:
            return Response({'files': 'Обязательное поле'}, status=400)

        try:
            payload = upload_chat_files(chat, request.user, uploaded_files)
        except PermissionError as exc:
            return Response({'detail': str(exc)}, status=403)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=400)

        return Response({'files': payload}, status=status.HTTP_201_CREATED)


class ChatMessageForwardView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, chat_id):
        serializer = ForwardMessagesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        from apps.chats.forward_services import ForwardError, forward_messages

        try:
            message = forward_messages(
                target_chat_id=chat_id,
                source_chat_id=serializer.validated_data['source_chat_id'],
                message_ids=serializer.validated_data['message_ids'],
                comment=serializer.validated_data.get('comment', ''),
                user=request.user,
            )
        except ForwardError as exc:
            return Response({'detail': exc.detail}, status=exc.status_code)

        return Response(
            MessageSerializer(message, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )


class ChatMessageDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, chat_id, message_id):
        try:
            chat = get_user_chats(request.user).get(id=chat_id)
        except Chat.DoesNotExist:
            return Response({'detail': 'Чат не найден'}, status=404)

        try:
            message = Message.objects.get(id=message_id, chat=chat)
        except Message.DoesNotExist:
            return Response({'detail': 'Сообщение не найдено'}, status=404)

        scope = request.data.get('scope', 'me')
        try:
            delete_message_for_user(message, request.user, scope)
        except PermissionError as exc:
            return Response({'detail': str(exc)}, status=403)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=400)

        return Response({'scope': scope}, status=status.HTTP_200_OK)


class ChatMessageRunView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, chat_id, message_id):
        rate_key = f'code_run_rate:{request.user.id}'
        if cache.get(rate_key):
            return Response(
                {'detail': f'Подождите {settings.CODE_RUN_RATE_LIMIT_SEC} сек перед следующим запуском'},
                status=429,
            )

        try:
            chat = get_user_chats(request.user).get(id=chat_id)
        except Chat.DoesNotExist:
            return Response({'detail': 'Чат не найден'}, status=404)

        try:
            message = Message.objects.get(id=message_id, chat=chat, deleted_at__isnull=True)
        except Message.DoesNotExist:
            return Response({'detail': 'Сообщение не найдено'}, status=404)

        file_name = (message.file_name or '').lower()
        mime = (message.mime_type or '').lower()
        is_py = file_name.endswith('.py') or mime.startswith('text/x-python') or 'python' in mime
        is_js = (
            file_name.endswith('.js')
            or mime in ('text/javascript', 'application/javascript', 'application/x-javascript', 'text/js')
            or 'javascript' in mime
        )
        if message.message_type != 'file' or not (is_py or is_js):
            return Response({'detail': 'Можно запускать только .py и .js файлы'}, status=400)

        try:
            source = download_object_bytes(
                message.content,
                max_bytes=settings.CODE_RUN_MAX_SOURCE_BYTES,
            )
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=400)

        if source is None:
            return Response({'detail': 'Не удалось скачать файл'}, status=404)

        cache.set(rate_key, 1, settings.CODE_RUN_RATE_LIMIT_SEC)

        try:
            if is_js:
                result = run_javascript_source(source, filename=message.file_name or 'script.js')
            else:
                result = run_python_source(source, filename=message.file_name or 'script.py')
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=400)

        return Response(result, status=status.HTTP_200_OK)


class UserSearchView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        import json
        from rest_framework.renderers import JSONRenderer

        q = request.query_params.get('q', '').strip()
        if len(q) < 2:
            return Response([])

        cache_key = f'user_search:{request.user.id}:{q.lower()}'
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        users = (
            User.objects.filter(
                Q(nickname__icontains=q)
                | Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
                | Q(email__icontains=q)
            )
            .exclude(id=request.user.id)
            .distinct()[:20]
        )
        payload = json.loads(
            JSONRenderer().render(
                UserSerializer(users, many=True, context={'request': request}).data
            )
        )
        cache.set(cache_key, payload, settings.USER_SEARCH_CACHE_TTL)
        return Response(payload)


class MediaProxyView(APIView):
    """Отдаёт объект MinIO через API (для телефонов, если localhost:MinIO недоступен)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        path = (request.query_params.get('path') or '').strip().lstrip('/')
        if not path or '/' not in path:
            return Response({'detail': 'path обязателен'}, status=400)

        bucket = path.split('/', 1)[0]
        allowed = {
            settings.MINIO_BUCKET_AVATARS,
            settings.MINIO_BUCKET_CHAT_FILES,
        }
        if bucket not in allowed:
            return Response({'detail': 'Недопустимый bucket'}, status=403)

        try:
            data = download_object_bytes(path, max_bytes=20 * 1024 * 1024)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=400)
        if data is None:
            return Response({'detail': 'Файл не найден'}, status=404)

        content_type = guess_type(path)[0] or 'application/octet-stream'
        response = HttpResponse(data, content_type=content_type)
        response['Cache-Control'] = 'private, max-age=3600'
        return response
