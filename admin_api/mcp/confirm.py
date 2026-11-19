"""Confirm tokens for ``require_dry_run``: single-use, issued by a dry run for exactly one write.

The token is a timestamp-signed random ``jti`` stored in :class:`admin_api.models.McpConfirmation`
with a SHA-256 fingerprint of the user, the API token, the operation, its exact arguments and the
stored state of the object it writes. The real write recomputes the fingerprint and spends the
row in one conditional UPDATE: a token works once, for that write, while the object is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import timedelta

from django.core import signing
from django.db import router
from django.utils import timezone

from ..errors import ApiError


class DryRunRequired(ApiError):
    status = 409
    code = "dry_run_required"


class InvalidConfirmToken(ApiError):
    status = 409
    code = "invalid_confirm_token"


def object_state(obj) -> dict | None:
    """The stored row of ``obj`` (all concrete fields, read fresh from the database), or ``None``."""
    if obj is None:
        return None
    model = type(obj)
    return model._base_manager.using(router.db_for_write(model)).filter(pk=obj.pk).values().first()


def _encode(value) -> str:
    return bytes(value).hex() if isinstance(value, (bytes, memoryview)) else str(value)


def fingerprint(request, operation: str, scope: dict, state) -> str:
    token = getattr(request, "api_token", None)
    body = [request.user.pk, getattr(token, "pk", None), operation, scope, state]
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=_encode).encode()).hexdigest()


def _model():
    from ..models import McpConfirmation

    return McpConfirmation


def issue(site, request, operation: str, scope: dict, state) -> str:
    model, now = _model(), timezone.now()
    model.objects.filter(expires_at__lt=now).delete()
    jti = secrets.token_urlsafe(32)
    expires_at = now + timedelta(seconds=max(site.confirm_token_max_age, 0))
    model.objects.create(jti=jti, fingerprint=fingerprint(request, operation, scope, state), expires_at=expires_at)
    return signing.TimestampSigner(salt=site.confirm_salt).sign(jti)


def verify(site, token, request, operation: str, scope: dict, state) -> None:
    """Spend ``token`` for this write or raise. A token that does not match is left unspent."""
    if not token:
        raise DryRunRequired(
            "This server requires a dry run first. Call again with dry_run=true, then repeat the call "
            "without dry_run and pass the confirm_token the dry run returned."
        )
    try:
        jti = signing.TimestampSigner(salt=site.confirm_salt).unsign(str(token), max_age=site.confirm_token_max_age)
    except signing.SignatureExpired:
        raise InvalidConfirmToken("The confirm_token has expired. Repeat the dry run.") from None
    except signing.BadSignature:
        jti = None
    model = _model()
    expected = fingerprint(request, operation, scope, state)
    if jti and model.objects.filter(jti=jti, fingerprint=expected, used_at__isnull=True).update(used_at=timezone.now()):
        return
    row = model.objects.filter(jti=jti).first() if jti else None
    if row is not None and row.fingerprint == expected:
        raise InvalidConfirmToken("The confirm_token was already used. Repeat the dry run.")
    raise InvalidConfirmToken(
        "The confirm_token does not belong to this write, or the object changed since the dry run. "
        "Repeat the dry run with exactly the arguments you are about to save."
    )
