from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from apps.users.views import (
    LoginView,
    MeAvatarView,
    MeView,
    RegisterAvatarView,
    RegisterCompleteView,
    RegisterPhoneView,
    RegisterProfileView,
    RegisterVerifyCodeView,
    TelegramWebhookView,
)

urlpatterns = [
    path('register/phone/', RegisterPhoneView.as_view()),
    # Legacy alias — same as phone registration.
    path('register/email/', RegisterPhoneView.as_view()),
    path('register/verify-code/', RegisterVerifyCodeView.as_view()),
    path('register/profile/', RegisterProfileView.as_view()),
    path('register/avatar/', RegisterAvatarView.as_view()),
    path('register/complete/', RegisterCompleteView.as_view()),
    path('telegram/webhook/', TelegramWebhookView.as_view()),
    path('login/', LoginView.as_view()),
    path('me/', MeView.as_view()),
    path('me/avatar/', MeAvatarView.as_view()),
    path('token/refresh/', TokenRefreshView.as_view()),
]
