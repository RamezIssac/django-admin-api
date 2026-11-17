"""Confirm tokens for ``require_dry_run``: a signed fingerprint of one write, issued by its dry run.

Stateless (the HTTP transport keeps no session between calls): the token is a timestamp-signed
SHA-256 of the user, the operation and its exact arguments. The real write recomputes the
fingerprint and refuses unless the token matches and is younger than ``max_age`` seconds.
"""

from __future__ import annotations

import hashlib
import json

from django.core import signing
from django.utils.crypto import constant_time_compare

from ..errors import ApiError


class DryRunRequired(ApiError):
    status = 409
    code = "dry_run_required"


class InvalidConfirmToken(ApiError):
    status = 409
    code = "invalid_confirm_token"


def fingerprint(request, operation: str, scope: dict) -> str:
    body = json.dumps([request.user.pk, operation, scope], sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()


def issue(salt: str, request, operation: str, scope: dict) -> str:
    return signing.TimestampSigner(salt=salt).sign(fingerprint(request, operation, scope))


def verify(salt: str, max_age: int, token, request, operation: str, scope: dict) -> None:
    if not token:
        raise DryRunRequired(
            "This server requires a dry run first. Call again with dry_run=true, then repeat the call "
            "without dry_run and pass the confirm_token the dry run returned."
        )
    try:
        signed = signing.TimestampSigner(salt=salt).unsign(str(token), max_age=max_age)
    except signing.SignatureExpired:
        raise InvalidConfirmToken("The confirm_token has expired. Repeat the dry run.") from None
    except signing.BadSignature:
        signed = None
    if signed is None or not constant_time_compare(signed, fingerprint(request, operation, scope)):
        raise InvalidConfirmToken(
            "The confirm_token does not belong to this write. Repeat the dry run with exactly the "
            "arguments you are about to save."
        )
