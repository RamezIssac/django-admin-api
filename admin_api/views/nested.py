"""REST routing for nested admins (see :mod:`admin_api.nested`).

``/v1/<app>/<model>/<pk>/<child>/[<pk>/<child>/…]`` followed by the same tails as a top-level
model: ``""`` (changelist), ``schema/``, ``actions/<name>/``, ``<pk>/``, ``<pk>/delete-preview/``.
"""

from __future__ import annotations

import re

from ..errors import MethodNotAllowed, NotFound
from .changelist import ActionView, ChangeListView, SchemaView
from .detail import DeletePreviewView, ObjectView

ROUTES = {
    "changelist": (ChangeListView, ("GET", "POST")),
    "schema": (SchemaView, ("GET",)),
    "action": (ActionView, ("POST",)),
    "object": (ObjectView, ("GET", "PATCH", "PUT", "DELETE")),
    "delete_preview": (DeletePreviewView, ("GET",)),
}
NESTED_METHODS = ("GET", "POST", "PATCH", "PUT", "DELETE")
_SEGMENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")  # a model_name


def parse_nested(nested: str) -> tuple[list[str], list[str], str, dict]:
    """``"1/sales/7/salesitem"`` → ``(["sales", "salesitem"], ["1", "7"], "changelist", {})``."""
    parts = nested.split("/")
    segments, parents = [], []

    def is_tail(rest):
        return len(rest) == 2 and (rest[0] == "actions" or rest[1] == "delete-preview")

    while len(parts) >= 2 and not is_tail(parts):
        parents.append(parts[0])
        segments.append(parts[1])
        parts = parts[2:]
    if not segments or not all(map(_SEGMENT_RE.match, segments)) or not all(parents) or not all(parts):
        raise NotFound("No such API endpoint.")
    if not parts:
        return segments, parents, "changelist", {}
    if parts == ["schema"]:
        return segments, parents, "schema", {}
    if len(parts) == 2 and parts[0] == "actions":
        return segments, parents, "action", {"action_name": parts[1]}
    if len(parts) == 1:
        return segments, parents, "object", {"object_id": parts[0]}
    return segments, parents, "delete_preview", {"object_id": parts[0]}


def nested_view(api_site):
    def view(request, app_label, model_name, nested):
        segments, parents, kind, kwargs = parse_nested(nested)
        view_class, methods = ROUTES[kind]
        if request.method not in methods:
            raise MethodNotAllowed(f"Allowed: {', '.join(methods)}")
        inner = view_class.as_view(api_site=api_site, segments=tuple(segments), parents=tuple(parents))
        return inner(request, app_label=app_label, model_name=model_name, **kwargs)

    return view
