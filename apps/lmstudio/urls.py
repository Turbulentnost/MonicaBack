from django.urls import path

from apps.lmstudio.views import (
    LmStudioChatCompletionsView,
    LmStudioHealthView,
    LmStudioModelsView,
)

urlpatterns = [
    path('health/', LmStudioHealthView.as_view()),
    path('v1/models/', LmStudioModelsView.as_view()),
    path('v1/chat/completions/', LmStudioChatCompletionsView.as_view()),
]
