from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.lmstudio.proxy import chat_completions, health, list_models


class LmStudioHealthView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        status_code, body = health()
        return Response(body, status=status_code)


class LmStudioModelsView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        status_code, body = list_models()
        return Response(body, status=status_code)


class LmStudioChatCompletionsView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        status_code, body = chat_completions(request.data if isinstance(request.data, dict) else {})
        return Response(body, status=status_code)
