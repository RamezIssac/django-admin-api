"""Run admin actions through the API.

``POST /v1/<app>/<model>/actions/<name>/`` with ``{"pks": [...]}`` or ``{"all": true}``
(``all`` honours the changelist filters passed as query parameters).

The action runs exactly like in the admin: same ``get_actions()`` (or the ``api_actions`` /
``get_api_actions`` twin), same permission filtering, same queryset (changelist filters +
selection), and a POST request carrying ``action``, ``_selected_action`` and ``post`` so
confirmation-style actions such as ``delete_selected`` complete.

An action that returns an ``HttpResponse`` (file export, intermediate page) cannot be
represented in JSON: its transaction is rolled back and ``400 action_not_supported`` is
returned. Give it an API twin, a method ``api_<action_name>(self, request, queryset)``
returning JSON-serializable data, to expose it.
"""

from __future__ import annotations

from django.contrib.admin import helpers
from django.core.exceptions import PermissionDenied
from django.db import router, transaction
from django.http import HttpResponseBase, QueryDict

from . import scopes
from .changelist import build_changelist, get_columns, split_api_params
from .encoders import encode_value
from .errors import ApiError, NotFound, PermissionDeniedError
from .writes import InvalidJson, as_post_request


class ActionNotSupported(ApiError):
    status = 400
    code = "action_not_supported"


class NothingSelected(ApiError):
    status = 400
    code = "nothing_selected"


def action_parts(action):
    """``(func, name, description)`` for Django < 6.1 tuples and >= 6.1 ``Action`` objects."""
    if hasattr(action, "func"):
        return action.func, action.name, action.description
    return action[0], action[1], action[2]


def api_twin(model_admin, name):
    twin = getattr(model_admin, f"api_{name}", None)
    return twin if callable(twin) else None


def describe_actions(model_admin, request) -> list[dict]:
    out = []
    for action in model_admin.get_actions(request).values():
        _func, name, description = action_parts(action)
        out.append({"name": name, "description": str(description), "api_twin": api_twin(model_admin, name) is not None})
    return out


def run_action(model_admin, request, name, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise InvalidJson("The action payload must be an object.")
    actions = model_admin.get_actions(request)
    if name not in actions:
        raise NotFound(f"No action {name!r} (or no permission).")
    func, name, _description = action_parts(actions[name])
    for perm in getattr(func, "allowed_permissions", ()):
        if perm in scopes.OPS:  # custom permissions are left to has_<perm>_permission
            scopes.check(request, model_admin.opts, perm)

    select_all = bool(payload.get("all"))
    pks = payload.get("pks") or []
    if not select_all and (not isinstance(pks, list) or not pks):
        raise NothingSelected('Send {"pks": [...]} or {"all": true}.')

    split_api_params(request)
    cl = build_changelist(model_admin, request, get_columns(model_admin, request))
    queryset = cl.get_queryset(request)
    if not select_all:
        queryset = queryset.filter(pk__in=pks)
    count = queryset.count()
    if not count:
        raise NothingSelected("No objects match the selection.")

    data = QueryDict(mutable=True)
    data["action"] = name
    data["post"] = "yes"
    data["index"] = "0"
    if select_all:
        data["select_across"] = "1"
    else:
        data.setlist(helpers.ACTION_CHECKBOX_NAME, [str(pk) for pk in pks])
    data._mutable = False
    post_request = as_post_request(request, data)

    twin = api_twin(model_admin, name)
    try:
        with transaction.atomic(using=router.db_for_write(model_admin.model)):
            result = twin(post_request, queryset) if twin else func(model_admin, post_request, queryset)
            if isinstance(result, HttpResponseBase):
                transaction.set_rollback(True)
    except PermissionDenied as exc:
        raise PermissionDeniedError(str(exc) or None) from exc
    if isinstance(result, HttpResponseBase):
        raise ActionNotSupported(
            f"Action {name!r} returned a page/file (e.g. a confirmation or export); nothing was changed. "
            f"Define api_{name}(self, request, queryset) on the admin to expose it in the API."
        )
    return {
        "action": name,
        "count": count,
        "messages": post_request._messages.captured(),
        "result": encode_value(result),
    }
