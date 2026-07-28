from django.urls import path

from apps.ai.views import CompleteView, StyleProfileView

urlpatterns = [
    path('complete/', CompleteView.as_view()),
    path('style/', StyleProfileView.as_view()),
]
