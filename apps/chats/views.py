from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.chats.models import Chat, Message
from apps.chats.serializers import MessageSerializer
from apps.chats.services import get_chat_partner, get_or_create_direct_chat, get_user_chats, user_in_chat
from apps.users.serializers import UserSerializer

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
            last_message = chat.messages.order_by('-sent_at').first()
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

        messages = chat.messages.select_related('sender').order_by('sent_at')
        page_size = int(request.query_params.get('limit', 50))
        offset = int(request.query_params.get('offset', 0))
        sliced = messages[offset:offset + page_size]
        return Response(MessageSerializer(sliced, many=True).data)


class UserSearchView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        q = request.query_params.get('q', '').strip()
        if len(q) < 2:
            return Response([])
        users = User.objects.filter(nickname__icontains=q).exclude(id=request.user.id)[:20]
        return Response(UserSerializer(users, many=True).data)
