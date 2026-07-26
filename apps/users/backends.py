from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model

from apps.users.services.phone import looks_like_phone, normalize_phone

User = get_user_model()


class LoginBackend(ModelBackend):
    """Authenticate by phone number or nickname (+ password)."""

    def authenticate(self, request, username=None, password=None, login=None, **kwargs):
        identifier = login or username or kwargs.get('email')
        if identifier is None or password is None:
            return None

        user = self._resolve_user(str(identifier).strip())
        if user is None:
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None

    def _resolve_user(self, identifier: str):
        if looks_like_phone(identifier):
            try:
                phone = normalize_phone(identifier)
            except Exception:
                return None
            return User.objects.filter(phone=phone).first()

        # Nickname (exact, case-insensitive)
        return User.objects.filter(nickname__iexact=identifier).first()

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None
