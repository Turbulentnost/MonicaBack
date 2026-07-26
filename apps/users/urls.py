from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from apps.users.views import (
    LoginView,
    MeAvatarView,
    MeView,
    RegisterAvatarView,
    RegisterCompleteView,
    RegisterEmailView,
    RegisterProfileView,
    RegisterVerifyCodeView,
)

urlpatterns = [
    path('register/email/', RegisterEmailView.as_view()),
    path('register/verify-code/', RegisterVerifyCodeView.as_view()),
    path('register/profile/', RegisterProfileView.as_view()),
    path('register/avatar/', RegisterAvatarView.as_view()),
    path('register/complete/', RegisterCompleteView.as_view()),
    path('login/', LoginView.as_view()),
    path('me/', MeView.as_view()),
    path('me/avatar/', MeAvatarView.as_view()),
    path('token/refresh/', TokenRefreshView.as_view()),
]
