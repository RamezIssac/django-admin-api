"""Personal access tokens. Built small, with room to grow (see docs/DESIGN.md §9).

Raw token format: ``aapi_<prefix>_<secret>``. Only ``sha256(secret)`` is stored; the raw
token is shown once, at creation.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

TOKEN_PREFIX = "aapi_"


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class ApiTokenManager(models.Manager):
    def create_token(self, user, name, *, expires_at=None, kind="pat", scopes=None):
        """Create a token. Returns ``(token, raw)``; ``raw`` is not stored anywhere."""
        token = self.model(user=user, name=name, expires_at=expires_at, kind=kind, scopes=scopes or [])
        raw = token.set_new_secret()
        token.save()
        return token, raw

    def get_for_raw(self, raw: str):
        """The active token matching ``raw``, or ``None``."""
        if not raw.startswith(TOKEN_PREFIX):
            return None
        prefix, sep, secret = raw[len(TOKEN_PREFIX) :].partition("_")
        if not sep or not prefix or not secret:
            return None
        token = self.select_related("user").filter(prefix=prefix).first()
        if token is None or not hmac.compare_digest(token.key_hash, hash_secret(secret)):
            return None
        return token if token.is_active else None


class AbstractApiToken(models.Model):
    KIND_CHOICES = [("pat", _("Personal access token")), ("mcp", _("MCP client")), ("service", _("Service"))]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_tokens", verbose_name=_("user")
    )
    name = models.CharField(_("name"), max_length=100, help_text=_("What is this token for?"))
    prefix = models.CharField(_("prefix"), max_length=16, unique=True, editable=False)
    key_hash = models.CharField(max_length=64, editable=False)
    created = models.DateTimeField(_("created"), auto_now_add=True)
    expires_at = models.DateTimeField(_("expires at"), null=True, blank=True)
    last_used_at = models.DateTimeField(_("last used at"), null=True, blank=True, editable=False)
    revoked_at = models.DateTimeField(_("revoked at"), null=True, blank=True, editable=False)
    # Reserved for later; present from day one so no data migration is needed.
    # scopes = [] means "the user's full admin permissions" (v1). Scopes will only narrow.
    scopes = models.JSONField(default=list, blank=True, editable=False)
    kind = models.CharField(_("kind"), max_length=20, choices=KIND_CHOICES, default="pat")

    objects = ApiTokenManager()

    class Meta:
        abstract = True
        ordering = ["-created"]
        verbose_name = _("API token")
        verbose_name_plural = _("API tokens")

    def __str__(self):
        return f"{self.name} ({TOKEN_PREFIX}{self.prefix}_…)"

    def set_new_secret(self) -> str:
        prefix = secrets.token_hex(4)
        while type(self).objects.filter(prefix=prefix).exists():
            prefix = secrets.token_hex(4)
        secret = secrets.token_urlsafe(32)
        self.prefix = prefix
        self.key_hash = hash_secret(secret)
        return f"{TOKEN_PREFIX}{prefix}_{secret}"

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= timezone.now()

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None and not self.is_expired

    def revoke(self):
        if self.revoked_at is None:
            self.revoked_at = timezone.now()
            self.save(update_fields=["revoked_at"])

    def touch(self, min_interval_seconds: int = 60):
        """Record usage, at most once per ``min_interval_seconds``."""
        now = timezone.now()
        if self.last_used_at is None or (now - self.last_used_at).total_seconds() >= min_interval_seconds:
            type(self).objects.filter(pk=self.pk).update(last_used_at=now)
            self.last_used_at = now


class ApiToken(AbstractApiToken):
    class Meta(AbstractApiToken.Meta):
        swappable = "ADMIN_API_TOKEN_MODEL"
