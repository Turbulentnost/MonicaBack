from django.urls import path

from apps.lmstudio.views import (
    LmStudioChatCompletionsView,
    LmStudioEmbeddingsView,
    LmStudioHealthView,
    LmStudioModelsView,
)

urlpatterns = [
    path('health/', LmStudioHealthView.as_view()),
    path('v1/models/', LmStudioModelsView.as_view()),
    path('v1/chat/completions/', LmStudioChatCompletionsView.as_view()),
    path('v1/embeddings/', LmStudioEmbeddingsView.as_view()),
]
