"""Twin resolution: ``get_api_X(request) → api_X → get_X(request) → X``.

Two entry points:

* :func:`resolve` implements the lookup order. It is used by ``ApiAdminMixin``'s routed
  getters and, for admins without the mixin, by the API views.
* :func:`api_value` is what API views call. For mixin admins it goes through the normal
  getter (``admin.get_X``) so that user overrides above the mixin in the MRO are honoured;
  the mixin then routes to the twin. For plain admins it calls :func:`resolve` directly.

Re-entrancy: a ``get_api_queryset`` that calls ``super().get_queryset(request)`` must get the
*normal* queryset, not recurse back into itself. While a twin method runs, its name is held in
a per-request stack and routed getters fall through to the normal implementation.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

# Hooks that ApiAdminMixin routes inside the normal Django getter (get_<name>).
ROUTED = frozenset(
    {
        "list_display",
        "list_filter",
        "search_fields",
        "ordering",
        "fields",
        "fieldsets",
        "readonly_fields",
        "inlines",
        "queryset",
    }
)

# Twins whose effect inside Django's form machinery requires the mixin (system check W001).
MIXIN_REQUIRED = frozenset({"fields", "fieldsets", "readonly_fields", "form", "inlines"})

_STACK_ATTR = "_admin_api_resolving"


def _stack(request) -> set:
    stack = getattr(request, _STACK_ATTR, None)
    if stack is None:
        stack = set()
        setattr(request, _STACK_ATTR, stack)
    return stack


def is_resolving(request, name: str) -> bool:
    return name in getattr(request, _STACK_ATTR, ())


@contextmanager
def resolving(request, name: str):
    stack = _stack(request)
    stack.add(name)
    try:
        yield
    finally:
        stack.discard(name)


def _normal(model_admin, name: str, request, *args, **kwargs) -> Any:
    """The non-API value: ``get_X(request, ...)`` if it exists, else attribute ``X``."""
    from .mixins import ApiAdminMixin

    getter_name = f"get_{name}"
    if isinstance(model_admin, ApiAdminMixin):
        # Skip the mixin's routing layer; we are already past any user override above it.
        getter = getattr(super(ApiAdminMixin, model_admin), getter_name, None)
    else:
        getter = getattr(model_admin, getter_name, None)
    if getter is not None:
        return getter(request, *args, **kwargs)
    return getattr(model_admin, name, None)


def resolve(model_admin, name: str, request, *args, **kwargs) -> Any:
    """Return the API value of hook ``name`` following the twin lookup order."""
    if not is_resolving(request, name):
        method = getattr(model_admin, f"get_api_{name}", None)
        if method is not None:
            with resolving(request, name):
                return method(request, *args, **kwargs)
        value = getattr(model_admin, f"api_{name}", None)
        if value is not None:
            return value
    return _normal(model_admin, name, request, *args, **kwargs)


def api_value(model_admin, name: str, request, *args, **kwargs) -> Any:
    """Entry point for API views."""
    from .mixins import ApiAdminMixin

    if isinstance(model_admin, ApiAdminMixin) and name in ROUTED:
        return getattr(model_admin, f"get_{name}")(request, *args, **kwargs)
    return resolve(model_admin, name, request, *args, **kwargs)


def defined_twins(model_admin) -> set[str]:
    """Names of hooks for which the admin defines an ``api_X`` attribute or ``get_api_X`` method."""
    names = set()
    for attr in dir(type(model_admin)):
        if attr.startswith("get_api_"):
            names.add(attr[len("get_api_") :])
        elif attr.startswith("api_") and getattr(model_admin, attr, None) is not None:
            names.add(attr[len("api_") :])
    return names
