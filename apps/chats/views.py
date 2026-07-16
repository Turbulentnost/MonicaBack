from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models import Q
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.chats.code_runner import run_javascript_source, run_python_source
from apps.chats.models import Chat, Message
from apps.chats.serializers import MessageSerializer
from apps.chats.services import (
    delete_message_for_user,
    get_chat_partner,
    get_last_visible_message,
    get_or_create_direct_chat,
    get_user_chats,
    get_visible_messages,
    upload_chat_files,
)
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
            'partner': UserSerializer(partner).data,
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
            result.append({
                'id': chat.id,
                'partner': UserSerializer(partner).data if partner else None,
                'last_message': MessageSerializer(last_message).data if last_message else None,
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

        messages = get_visible_messages(chat, request.user).order_by('sent_at')
        page_size = int(request.query_params.get('limit', 50))
        offset = int(request.query_params.get('offset', 0))
        sliced = messages[offset:offset + page_size]
        return Response(MessageSerializer(sliced, many=True).data)


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
        payload = json.loads(JSONRenderer().render(UserSerializer(users, many=True).data))
        cache.set(cache_key, payload, settings.USER_SEARCH_CACHE_TTL)
        return Response(payload)
