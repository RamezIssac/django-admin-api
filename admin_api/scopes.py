"""Token scopes: narrow what an API token may do below its user's admin permissions.

A scope is ``"<app_label>.<model_name>:<op>"`` with ``op`` one of ``view``, ``add``,
``change``, ``delete``; any part may be ``*``. Examples: ``store.sales:view``,
``store.*:view``, ``cash.*:*``, ``*.*:view``.

* A token with ``scopes == []`` has its user's full permissions (unchanged behaviour).
* Scopes only *narrow*: ``has_*_permission`` stays the ceiling, a scope never grants anything.
* As in Django, ``change`` implies ``view``.

Enforcement is in :class:`admin_api.mixins.ApiAdminMixin` (``has_*_permission``, and scoped
inline classes so formset permissions follow too) and, as a second line, in
:mod:`admin_api.services`.
"""

from __future__ import annotations

import re

from django.core.exceptions import ValidationError

OPS = ("view", "add", "change", "delete")
_SCOPE_RE = re.compile(r"^(\*|[a-z_][a-z0-9_]*)\.(\*|[a-z_][a-z0-9_]*):(\*|view|add|change|delete)$")


def parse_scope(scope: str) -> tuple[str, str, str]:
    match = _SCOPE_RE.match((scope or "").strip().lower())
    if not match:
        raise ValidationError(
            "Invalid scope %(scope)r. Expected '<app_label>.<model_name>:<view|add|change|delete>', "
            "'*' allowed in any part.",
            params={"scope": scope},
            code="invalid_scope",
        )
    return match.group(1), match.group(2), match.group(3)


def normalize_scopes(scopes) -> list[str]:
    """Validate and normalize (lower-case, stripped, de-duplicated, order kept)."""
    if scopes is None:
        return []
    if isinstance(scopes, str):
        scopes = scopes.replace(",", "\n").splitlines()
    out: list[str] = []
    for scope in scopes:
        if not str(scope).strip():
            continue
        app, model, op = parse_scope(str(scope))
        normalized = f"{app}.{model}:{op}"
        if normalized not in out:
            out.append(normalized)
    return out


def scopes_allow(scopes, app_label: str, model_name: str, op: str) -> bool:
    if not scopes:
        return True
    wanted = {op, "change"} if op == "view" else {op}
    for scope in scopes:
        try:
            s_app, s_model, s_op = parse_scope(scope)
        except ValidationError:
            continue  # an invalid stored scope grants nothing
        if s_app not in ("*", app_label) or s_model not in ("*", model_name):
            continue
        if s_op == "*" or s_op in wanted:
            return True
    return False


def request_scopes(request):
    """The scopes restricting ``request``, or ``None`` when unrestricted."""
    token = getattr(request, "api_token", None)
    scopes = getattr(token, "scopes", None) if token is not None else None
    return scopes or None


def request_allows(request, opts, op: str) -> bool:
    """Do the request's token scopes allow ``op`` on the model with ``_meta`` ``opts``?"""
    scopes = request_scopes(request) if request is not None else None
    if scopes is None:
        return True
    return scopes_allow(scopes, opts.app_label, opts.model_name, op)


def check(request, opts, op: str):
    from .errors import PermissionDeniedError

    if not request_allows(request, opts, op):
        raise PermissionDeniedError(f"Token scopes do not allow '{op}' on {opts.app_label}.{opts.model_name}.")


class ScopedInlineMixin:
    """Mixed into inline classes for scoped requests (see ``ApiAdminMixin.get_inlines``)."""

    def _scope_opts(self):
        # auto-created M2M "through" inlines are governed by the parent model, as in Django
        return self.parent_model._meta if self.opts.auto_created else self.opts

    def has_view_permission(self, request, obj=None):
        return super().has_view_permission(request, obj) and request_allows(request, self._scope_opts(), "view")

    def has_add_permission(self, request, obj=None):
        return super().has_add_permission(request, obj) and request_allows(request, self._scope_opts(), "add")

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and request_allows(request, self._scope_opts(), "change")

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and request_allows(request, self._scope_opts(), "delete")


_scoped_inline_classes: dict = {}


def scoped_inline_class(inline_class):
    cls = _scoped_inline_classes.get(inline_class)
    if cls is None:
        cls = type(f"Scoped{inline_class.__name__}", (ScopedInlineMixin, inline_class), {"__module__": __name__})
        _scoped_inline_classes[inline_class] = cls
    return cls
