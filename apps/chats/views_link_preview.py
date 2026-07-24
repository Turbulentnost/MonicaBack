from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.chats.link_preview import fetch_link_preview, validate_preview_url


class LinkPreviewView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        raw = (request.query_params.get('url') or '').strip()
        if not raw:
            return Response({'detail': 'url обязателен'}, status=400)
        if not validate_preview_url(raw):
            return Response({'detail': 'Некорректный или недоступный URL'}, status=400)

        data = fetch_link_preview(raw)
        if not data:
            return Response({'detail': 'Не удалось получить превью'}, status=404)
        return Response(data)
