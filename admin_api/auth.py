"""Pluggable authenticators. Each returns a user or ``None`` (= not mine, try the next one)."""

from __future__ import annotations

from django.middleware.csrf import CsrfViewMiddleware

from .errors import PermissionDeniedError

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
