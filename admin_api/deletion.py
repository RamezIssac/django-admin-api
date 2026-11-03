from __future__ import annotations

import html

from django.db import router, transaction
from django.utils.html import strip_tags

from .encoders import encode_value
from .errors import ApiError, PermissionDeniedError


class Protected(ApiError):
    status = 409
    code = "protected"


def _text(value):
    if isinstance(value, (list, tuple)):
        return [_text(v) for v in value]
    return html.unescape(strip_tags(str(value)))


def delete_preview(model_admin, request, obj) -> dict:
    to_delete, model_count, perms_needed, protected = model_admin.get_deleted_objects([obj], request)
    return {
        "object": encode_value(obj),
        "to_delete": _text(to_delete),
        "model_count": {str(k): v for k, v in model_count.items()},
        "perms_needed": sorted(str(p) for p in perms_needed),
        "protected": _text(list(protected)),
        "can_delete": not perms_needed and not protected,
    }


def _log_deletion(model_admin, request, obj):
    if hasattr(model_admin, "log_deletions"):  # Django >= 5.1
        model_admin.log_deletions(request, [obj])
    else:  # pragma: no cover - Django < 5.1
        model_admin.log_deletion(request, obj, str(obj))


def delete_object(model_admin, request, obj) -> dict:
    preview = delete_preview(model_admin, request, obj)
    if preview["protected"]:
        raise Protected("Related objects are protected.", protected=preview["protected"])
    if preview["perms_needed"]:
        raise PermissionDeniedError(
            "Missing delete permission on related objects.", perms_needed=preview["perms_needed"]
        )
    deleted = encode_value(obj)
    with transaction.atomic(using=router.db_for_write(model_admin.model)):
        _log_deletion(model_admin, request, obj)
        model_admin.delete_model(request, obj)
    return deleted
