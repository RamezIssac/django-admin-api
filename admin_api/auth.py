"""Pluggable authenticators. Each returns a user or ``None`` (= not mine, try the next one)."""

from __future__ import annotations

from django.middleware.csrf import CsrfViewMiddleware

from .errors import NotAuthenticated, PermissionDeniedError

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


class BaseAuthenticator:
    def authenticate(self, request):  # pragma: no cover - interface
        raise NotImplementedError


class _CsrfCheck(CsrfViewMiddleware):
    def _reject(self, request, reason):
        return reason


class SessionAuthenticator(BaseAuthenticator):
    """Uses the Django session (same login as the admin). Enforces CSRF on unsafe methods."""

    def authenticate(self, request):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None
        if request.method not in SAFE_METHODS:
            reason = _CsrfCheck(lambda r: None).process_view(request, None, (), {})
            if reason:
                raise PermissionDeniedError(f"CSRF failed: {reason}", code="csrf_failed")
        return user


def get_token_model():
    from django.apps import apps
    from django.conf import settings

    return apps.get_model(getattr(settings, "ADMIN_API_TOKEN_MODEL", "admin_api.ApiToken"), require_ready=True)


class TokenAuthenticator(BaseAuthenticator):
    """``Authorization: Bearer aapi_<prefix>_<secret>``. No CSRF (not cookie based).

    A request that sends a Bearer token is never authenticated by the session: an invalid,
    revoked or expired token is a 401, not a fall-through.
    """

    keyword = "bearer"

    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        scheme, _, raw = header.partition(" ")
        if scheme.lower() != self.keyword:
            return None
        token = get_token_model().objects.get_for_raw(raw.strip())
        if token is None or not token.user.is_active:
            raise NotAuthenticated("Invalid, revoked or expired token.", code="invalid_token")
        token.touch()
        request.api_token = token
        return token.user
