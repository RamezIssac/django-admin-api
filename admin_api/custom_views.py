"""Custom admin views in the API and MCP: ``@admin_api.view``.

A custom admin page (a statement, a stock balance, "post this invoice"…) usually has two
parts: computing data and rendering HTML. Put the first part in a ModelAdmin method decorated
with :func:`admin_api.view`; the HTML view and the API then share it::

    class ClientAdmin(admin.ModelAdmin):
        @admin_api.view(detail=True, params=StatementForm, description="Account statement.")
        def statement(self, request, obj, date_from=None, date_to=None):
            return {...}  # JSON-able (models, dates, Decimals are encoded)

* ``detail=True``: runs on one object, ``method(request, obj, **params)``;
  REST ``/v1/<app>/<model>/<pk>/views/<name>/``.
  ``detail=False``: ``method(request, **params)``; REST ``/v1/<app>/<model>/views/<name>/``.
* ``params``: a Django ``Form`` class (validated, ``cleaned_data`` passed as keyword arguments,
  JSON Schema generated from it) or a dict of JSON Schema properties (with ``required=[...]``).
* ``permissions``: like ``@admin.action(permissions=...)``: every ``has_<perm>_permission`` must
  pass (``view`` means view-or-change; with ``detail=True`` the object is passed for the
  built-in ones). Token scopes apply to the built-in ones.
* ``writes``: defaults to ``True`` when ``permissions`` asks for more than ``view``
  (``writes=True`` with the default permissions means ``permissions=["change"]``). Write views
  are POST only, run in a transaction, need a write scope on tokens, are hidden from read-only
  MCP sites and support ``dry_run`` in MCP. Read views accept GET (query string) and POST (JSON).
* MCP: listed in ``list_models`` and ``describe_model``, run with the ``run_view`` tool;
  ``mcp_tool="name"`` also publishes the view as its own MCP tool (top-level admins only).
* ``api_enabled=False`` hides the view from both surfaces, ``mcp_enabled=False`` from MCP only.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from django import forms
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.db import router, transaction
from django.http import HttpResponseBase, QueryDict
from django.utils.dateparse import parse_date, parse_datetime, parse_time

from . import scopes
from .encoders import encode_value
from .errors import ApiError, NotFound, PermissionDeniedError

VIEW_ATTR = "admin_api_view"
READ_METHODS = ("GET", "POST")
WRITE_METHODS = ("POST",)
# Reserved keys of a promoted MCP tool's arguments (params are flattened next to them).
TOOL_RESERVED = frozenset({"pk", "dry_run"})


class InvalidParams(ApiError):
    status = 400
    code = "invalid_params"


class ViewNotSupported(ApiError):
    status = 400
    code = "view_not_supported"


@dataclass(frozen=True)
class ViewSpec:
    """What ``@admin_api.view`` attaches to a ModelAdmin method."""

    name: str
    attname: str  # the method name on the admin
    detail: bool = False
    permissions: tuple = ("view",)
    writes: bool = False
    form_class: type | None = None
    properties: dict = field(default_factory=dict)  # JSON Schema properties, when not a form
    required: tuple = ()
    title: str = ""
    description: str = ""
    output_schema: dict | None = None
    api_enabled: bool = True
    mcp_enabled: bool = True
    mcp_tool: str | None = None

    @property
    def methods(self) -> tuple:
        return WRITE_METHODS if self.writes else READ_METHODS

    def params_schema(self) -> dict:
        """JSON Schema (object) of the parameters."""
        if self.form_class is not None:
            from .jsonschema import form_object_schema

            return form_object_schema(self.form_class, required=True)
        out = {"type": "object", "properties": dict(self.properties), "additionalProperties": False}
        if self.required:
            out["required"] = list(self.required)
        return out

    def describe(self) -> dict:
        out = {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "detail": self.detail,
            "writes": self.writes,
            "permissions": list(self.permissions),
            "params_schema": self.params_schema(),
        }
        if self.output_schema is not None:
            out["output_schema"] = self.output_schema
        if self.mcp_tool:
            out["mcp_tool"] = self.mcp_tool
        return out


def view(
    function=None,
    *,
    name: str | None = None,
    detail: bool = False,
    permissions=("view",),
    writes: bool | None = None,
    params=None,
    required=(),
    title: str | None = None,
    description: str | None = None,
    output_schema: dict | None = None,
    api_enabled: bool = True,
    mcp_enabled: bool = True,
    mcp_tool: str | None = None,
):
    """Expose a ModelAdmin method as a custom view in the REST API and MCP. See the module docs."""

    def decorator(func):
        permissions_ = (permissions,) if isinstance(permissions, str) else tuple(permissions)
        if writes and permissions_ == ("view",):
            permissions_ = ("change",)  # a write view does not run on view permission alone
        if not permissions_:
            raise ImproperlyConfigured(f"@admin_api.view {func.__name__}: permissions cannot be empty.")
        form_class, properties = None, {}
        if isinstance(params, type) and issubclass(params, forms.BaseForm):
            form_class = params
            if required:
                raise ImproperlyConfigured(f"@admin_api.view {func.__name__}: 'required' comes from the form.")
        elif isinstance(params, dict):
            properties = dict(params)
            unknown = set(required) - set(properties)
            if unknown:
                raise ImproperlyConfigured(
                    f"@admin_api.view {func.__name__}: required {sorted(unknown)} not in params."
                )
        elif params is not None:
            raise ImproperlyConfigured(
                f"@admin_api.view {func.__name__}: params must be a Form class or a dict of JSON Schema properties."
            )
        names = set(form_class.base_fields) if form_class is not None else set(properties)
        if mcp_tool and names & TOOL_RESERVED:
            raise ImproperlyConfigured(
                f"@admin_api.view {func.__name__}: params {sorted(names & TOOL_RESERVED)} clash with the arguments "
                f"of the MCP tool {mcp_tool!r}; rename them."
            )
        doc = (func.__doc__ or "").strip()
        spec = ViewSpec(
            name=name or func.__name__,
            attname=func.__name__,
            detail=detail,
            permissions=permissions_,
            writes=(set(permissions_) != {"view"}) if writes is None else bool(writes),
            form_class=form_class,
            properties=properties,
            required=tuple(required),
            title=str(title or (name or func.__name__).replace("_", " ").capitalize()),
            description=str(description or doc),
            output_schema=output_schema,
            api_enabled=api_enabled,
            mcp_enabled=mcp_enabled,
            mcp_tool=mcp_tool,
        )
        setattr(func, VIEW_ATTR, spec)
        return func

    if function is None:
        return decorator
    return decorator(function)


# --------------------------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------------------------
_class_cache: dict = {}


def declared_views(model_admin) -> dict[str, ViewSpec]:
    """``{name: spec}`` of the views declared on the admin's class (subclasses override)."""
    cls = type(model_admin)
    cached = _class_cache.get(cls)
    if cached is None:
        cached = {}
        for klass in reversed(cls.__mro__):
            for attr, value in vars(klass).items():
                spec = getattr(value, VIEW_ATTR, None)
                if isinstance(spec, ViewSpec):
                    # an override without the decorator removes the view
                    cached = {n: s for n, s in cached.items() if s.attname != attr}
                    cached[spec.name] = spec
                elif attr in {s.attname for s in cached.values()}:
                    cached = {n: s for n, s in cached.items() if s.attname != attr}
        _class_cache[cls] = cached
    return cached


def has_view_permission(model_admin, request, spec: ViewSpec, obj=None) -> bool:
    """Every permission of ``spec`` (and the token scopes of the built-in ones)."""
    for perm in spec.permissions:
        if perm == "view":
            ok = model_admin.has_view_or_change_permission(request, obj)
        elif perm == "add":
            ok = model_admin.has_add_permission(request)
        elif perm in ("change", "delete"):
            ok = getattr(model_admin, f"has_{perm}_permission")(request, obj)
        else:  # custom, like admin actions: has_<perm>_permission(request)
            checker = getattr(model_admin, f"has_{perm}_permission", None)
            if checker is None:
                raise ImproperlyConfigured(
                    f"{type(model_admin).__name__} has no has_{perm}_permission() for view {spec.name!r}."
                )
            ok = checker(request)
        if not ok:
            return False
    return all(scopes.request_allows(request, model_admin.opts, op) for op in scope_ops(spec))


def scope_ops(spec: ViewSpec) -> set:
    """Token scope operations a view needs: its built-in permissions; ``change`` for a write view
    that only names custom ones (a view-only token never runs a write)."""
    ops = {p for p in spec.permissions if p in scopes.OPS}
    if spec.writes and not ops - {"view"}:
        ops.add("change")
    return ops


def available_views(model_admin, request, *, mcp: bool = False, read_only: bool = False) -> list[ViewSpec]:
    """The views ``request.user`` may run on this admin (detail views checked without an object)."""
    out = []
    for spec in declared_views(model_admin).values():
        if not spec.api_enabled or (mcp and not spec.mcp_enabled) or (read_only and spec.writes):
            continue
        if has_view_permission(model_admin, request, spec):
            out.append(spec)
    return out


def get_view(model_admin, request, name: str, *, mcp: bool = False, read_only: bool = False) -> ViewSpec:
    spec = declared_views(model_admin).get(name)
    if spec is None or not spec.api_enabled or (mcp and not spec.mcp_enabled) or (read_only and spec.writes):
        raise NotFound(f"No view {name!r} on {model_admin.opts.app_label}.{model_admin.opts.model_name}.")
    return spec


# --------------------------------------------------------------------------------------------
# parameters
# --------------------------------------------------------------------------------------------
_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}
_FORMATS = {"date": parse_date, "date-time": parse_datetime, "time": parse_time}


def _types(schema) -> list:
    t = schema.get("type")
    return t if isinstance(t, list) else ([t] if t else [])


def _from_query(raw: list[str], schema: dict):
    """Query-string values (strings) → the JSON type the schema asks for."""
    types = _types(schema)
    if "array" in types:
        items = schema.get("items") or {}
        return [_from_query([v], items) for v in raw]
    value = raw[-1]
    if value == "" and "null" in types:
        return None
    for t in types:
        try:
            if t == "integer":
                return int(value)
            if t == "number":
                return float(value) if any(c in value for c in ".eE") else int(value)
            if t == "boolean" and value.lower() in ("1", "true", "yes", "on", "0", "false", "no", "off"):
                return value.lower() in ("1", "true", "yes", "on")
        except ValueError:
            continue
    return value


def _check_value(name: str, value, schema: dict, errors: dict):
    types = _types(schema)
    if types and not any(_TYPES.get(t, lambda v: True)(value) for t in types):
        errors.setdefault(name, []).append(f"Expected {' or '.join(types)}.")
        return value
    if "enum" in schema and value not in schema["enum"]:
        errors.setdefault(name, []).append(f"Must be one of: {', '.join(map(str, schema['enum']))}.")
        return value
    if isinstance(value, list) and "items" in schema:
        return [_check_value(f"{name}[{i}]", v, schema["items"], errors) for i, v in enumerate(value)]
    parser = _FORMATS.get(schema.get("format"))
    if parser is not None and isinstance(value, str):
        try:
            parsed = parser(value)
        except ValueError:
            parsed = None
        if parsed is None:
            errors.setdefault(name, []).append(f"Not a valid {schema['format']}.")
        return parsed
    for key, test, msg in (
        ("minimum", lambda v, m: v >= m, "at least"),
        ("maximum", lambda v, m: v <= m, "at most"),
    ):
        if key in schema and isinstance(value, (int, float)) and not test(value, schema[key]):
            errors.setdefault(name, []).append(f"Must be {msg} {schema[key]}.")
    return value


def _clean_schema_params(spec: ViewSpec, data: dict) -> dict:
    errors: dict = {}
    for key in data:
        if key not in spec.properties:
            errors.setdefault(key, []).append("Unknown parameter.")
    out = {}
    for key, schema in spec.properties.items():
        if key in data:
            out[key] = _check_value(key, data[key], schema, errors)
        elif key in spec.required:
            errors.setdefault(key, []).append("This parameter is required.")
        elif "default" in schema:
            out[key] = _check_value(key, schema["default"], schema, errors)
    if errors:
        raise InvalidParams("Invalid parameters.", fields=errors)
    return out


def _clean_form_params(spec: ViewSpec, request, data) -> dict:
    from .writes import field_post_values

    base_fields = spec.form_class.base_fields
    if isinstance(data, QueryDict):
        query = data
        unknown = [k for k in query if k not in base_fields and not any(k.startswith(f"{f}_") for f in base_fields)]
    else:
        query = QueryDict(mutable=True)
        unknown = [k for k in data if k not in base_fields]
        for key, value in data.items():
            if key in base_fields:
                for post_key, values in field_post_values(key, base_fields[key], value).items():
                    query.setlist(post_key, values)
    if unknown:
        raise InvalidParams("Invalid parameters.", fields={k: ["Unknown parameter."] for k in unknown})
    form = spec.form_class(data=query)
    if not form.is_valid():
        errors = {k: [str(m) for m in v] for k, v in form.errors.items()}
        raise InvalidParams("Invalid parameters.", fields=errors)
    return dict(form.cleaned_data)


def clean_params(spec: ViewSpec, request, data) -> dict:
    """Validate ``data`` (a JSON object, or the query string of a GET as a ``QueryDict``) into the
    keyword arguments of the view method."""
    if spec.form_class is not None:
        return _clean_form_params(spec, request, data)
    if isinstance(data, QueryDict):
        data = {k: _from_query(data.getlist(k), spec.properties.get(k, {})) for k in data}
    return _clean_schema_params(spec, data)


# --------------------------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------------------------
def run_view(model_admin, request, name: str, *, object_id=None, data=None, mcp=False, read_only=False) -> dict:
    """Check permissions, validate ``data`` and run the view. Returns
    ``{"view", "result", "messages"}`` (plus ``"pk"`` for a detail view)."""
    from .services import get_object
    from .writes import capture_messages

    spec = get_view(model_admin, request, name, mcp=mcp, read_only=read_only)
    obj = None
    if spec.detail:
        if object_id is None:
            raise InvalidParams(f"View {name!r} runs on one object: give its pk.")
        obj = get_object(model_admin, request, object_id)
    elif object_id is not None:
        raise InvalidParams(f"View {name!r} does not run on an object: no pk expected.")
    if not has_view_permission(model_admin, request, spec, obj):
        raise PermissionDeniedError()
    for op in scope_ops(spec):  # second line of defense, like the other services
        scopes.check(request, model_admin.opts, op)
    kwargs = clean_params(spec, request, data if data is not None else {})

    # a copy, so messages raised by the view are captured and returned instead of stored
    request = copy.copy(request)
    storage = capture_messages(request)
    method = getattr(model_admin, spec.attname)
    args = (request, obj) if spec.detail else (request,)
    try:
        if spec.writes:
            with transaction.atomic(using=router.db_for_write(model_admin.model)):
                result = method(*args, **kwargs)
                if isinstance(result, HttpResponseBase):
                    transaction.set_rollback(True)
        else:
            result = method(*args, **kwargs)
    except DjangoPermissionDenied as exc:
        raise PermissionDeniedError(str(exc) or None) from exc
    except ValidationError as exc:
        raise ApiError(
            "; ".join(exc.messages), status=400, code="validation_failed", non_field_errors=exc.messages
        ) from exc
    if isinstance(result, HttpResponseBase):
        raise ViewNotSupported(
            f"View {name!r} returned a page/file; nothing was changed. Return JSON-able data from the "
            "@admin_api.view method and render it in your HTML view instead."
        )
    out = {"view": spec.name}
    if spec.detail:
        out["pk"] = encode_value(obj.pk)
    out["result"] = encode_value(result)
    messages = storage.captured()
    if messages:
        out["messages"] = messages
    return out


def view_url(spec: ViewSpec, base_url: str | None, pk=None) -> str | None:
    """REST url of a view under the changelist url ``base_url``."""
    if base_url is None:
        return None
    if spec.detail:
        if pk is None:
            return None
        from django.contrib.admin.utils import quote

        return f"{base_url}{quote(str(pk))}/views/{spec.name}/"
    return f"{base_url}views/{spec.name}/"


__all__ = [
    "InvalidParams",
    "ViewNotSupported",
    "ViewSpec",
    "available_views",
    "clean_params",
    "declared_views",
    "get_view",
    "run_view",
    "view",
]
