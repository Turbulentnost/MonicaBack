from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model

User = get_user_model()


class LoginBackend(ModelBackend):
    """Authenticate by email or nickname (+ password)."""

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
        if '@' in identifier:
            return User.objects.filter(email__iexact=identifier).first()
        return User.objects.filter(nickname__iexact=identifier).first()

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None
