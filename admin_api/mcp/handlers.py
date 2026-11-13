"""The MCP tools: thin adapters over :mod:`admin_api.services`.

Each handler is ``handler(mcp_site, request, arguments) -> dict``. They run synchronously in
a Django request context and raise :class:`admin_api.errors.ApiError` on failure.
"""

from __future__ import annotations

import copy

from django.contrib.admin.views.main import ORDER_VAR, PAGE_VAR, SEARCH_VAR
from django.db import router, transaction
from django.http import QueryDict

from .. import custom_views, services
from ..changelist import PER_PAGE_PARAM, SHAPE_PARAM, describe_columns, get_columns
from ..errors import ApiError, NotFound, PermissionDeniedError
from ..nested import ModelPath
from ..writes import ValidationFailed
from .tools import (
    ANY_OBJECT,
    DATA_ARG,
    DRY_RUN_ARG,
    FILTERS_ARG,
    MODEL_ARG,
    PARENTS_ARG,
    PK_ARG,
    ToolSpec,
    obj,
)

RESERVED_FILTER_KEYS = frozenset({SEARCH_VAR, ORDER_VAR, PAGE_VAR, SHAPE_PARAM, PER_PAGE_PARAM, "all"})


class InvalidArguments(ApiError):
    status = 400
    code = "invalid_arguments"


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------
def _arg(args: dict, name: str, types=None, *, required=True, default=None):
    value = args.get(name)
    if value is None:
        if required:
            raise InvalidArguments(f"'{name}' is required.")
        return default
    if types is not None and not isinstance(value, types):
        raise InvalidArguments(f"'{name}' has the wrong type.")
    return value


def _model_admin(site, request, args):
    """The admin for ``model`` (a model path) inside ``parents``. Hiding an admin from MCP hides
    the admins nested under it too."""
    path = ModelPath.parse(_arg(args, "model", str))
    parents = _arg(args, "parents", list, required=False, default=[])
    if any(not isinstance(pk, (int, str)) or isinstance(pk, bool) for pk in parents):
        raise InvalidArguments("'parents' must be a list of primary keys.")
    chain = site.api_site.get_admin_chain(path)
    if chain is not None and not all(site.is_exposed(a) for a in chain):
        raise NotFound(f"No MCP access to {path.label}.")
    model_admin = services.get_model_admin_by_label(site.api_site, request, path.label, parents)
    return model_admin, path.label


def _children(site, request, model_admin, pk=None) -> list[dict]:
    return [link for link in services.child_links(request, model_admin, pk) if site.is_exposed(link["admin"])]


def _with_query(request, query: QueryDict):
    """A shallow copy of ``request`` whose GET is ``query`` (the changelist reads request.GET)."""
    req = copy.copy(request)
    query._mutable = False
    req.GET = query
    return req


def _qvalue(value) -> str:
    if value is True:
        return "1"
    if value is False:
        return "0"
    return "" if value is None else str(value)


def _filters_query(args) -> QueryDict:
    filters = _arg(args, "filters", dict, required=False, default={})
    query = QueryDict(mutable=True)
    for key, value in filters.items():
        if key in RESERVED_FILTER_KEYS:
            raise InvalidArguments(f"'{key}' is not a filter; use the dedicated argument instead.")
        values = value if isinstance(value, list) else [value]
        query.setlist(str(key), [_qvalue(v) for v in values])
    return query


def _effective_perms(site, perms: dict) -> dict:
    if site.read_only:
        return {k: (v if k == "view" else False) for k, v in perms.items()}
    return perms


def _ordering_param(model_admin, request, ordering) -> str:
    names = ordering if isinstance(ordering, list) else str(ordering).split(",")
    columns = {c["name"]: c for c in describe_columns(get_columns(model_admin, request), model_admin, request)}
    sortable = sorted(n for n, c in columns.items() if c["sortable"])
    parts = []
    for raw in names:
        name = str(raw).strip()
        desc = name.startswith("-")
        name = name.lstrip("-")
        column = columns.get(name)
        if column is None or not column["sortable"]:
            raise InvalidArguments(f"Cannot order by {name!r}. Sortable columns: {', '.join(sortable) or 'none'}.")
        parts.append(f"{'-' if desc else ''}{column['order_index']}")
    return ".".join(parts)


def _ensure_writable(site):
    if site.read_only:
        raise PermissionDeniedError("This MCP server is read-only.", code="read_only")


def _write_result(data: dict) -> dict:
    messages = data.pop("messages", None)
    out = {"object": data}
    if messages:
        out["messages"] = messages
    return out


def _dry_run(model_admin, request, fn) -> dict:
    """Run ``fn`` (a write service) and roll everything back, including on_commit callbacks."""
    request.is_dry_run = True
    try:
        with transaction.atomic(using=router.db_for_write(model_admin.model)):
            try:
                out = {"dry_run": True, "valid": True, **_write_result(fn())}
            except ValidationFailed as exc:
                out = {"dry_run": True, "valid": False, "errors": exc.extra}
            transaction.set_rollback(True)
    finally:
        request.is_dry_run = False
    if out["valid"]:
        out["note"] = "Nothing was saved; the pk shown is provisional."
    return out


def _views(site, request, model_admin) -> list:
    """The custom views of ``model_admin`` the user may run through this MCP site."""
    return [
        spec
        for spec in custom_views.available_views(model_admin, request, mcp=True, read_only=site.read_only)
        if site.is_view_exposed(model_admin, spec)
    ]


def _view_summary(spec) -> dict:
    out = {"name": spec.name, "description": spec.description or spec.title, "detail": spec.detail}
    if spec.writes:
        out["writes"] = True
    if spec.mcp_tool:
        out["mcp_tool"] = spec.mcp_tool
    return out


def _describe_view(spec) -> dict:
    out = spec.describe()
    out.pop("permissions", None)
    if not spec.mcp_tool:
        out.pop("mcp_tool", None)
    return out


def execute_view(site, request, model_admin, name, *, pk=None, params=None, dry_run=False) -> dict:
    """Run a custom view for MCP (``run_view`` and the promoted view tools)."""
    spec = custom_views.get_view(model_admin, request, name, mcp=True, read_only=site.read_only)
    if not site.is_view_exposed(model_admin, spec):
        raise NotFound(f"No view {name!r}.")
    if params is not None and not isinstance(params, dict):
        raise InvalidArguments("'params' must be an object.")

    def run():
        return services.run_view(model_admin, request, name, object_id=pk, data=params or {})

    if not dry_run:
        return run()
    if not spec.writes:
        return {**run(), "dry_run": True, "note": "A read-only view; nothing to roll back."}
    request.is_dry_run = True
    try:
        with transaction.atomic(using=router.db_for_write(model_admin.model)):
            out = {**run(), "dry_run": True, "note": "Nothing was saved: the view ran and was rolled back."}
            transaction.set_rollback(True)
    finally:
        request.is_dry_run = False
    return out


# --------------------------------------------------------------------------------------------
# handlers
# --------------------------------------------------------------------------------------------
def list_models(site, request, args) -> dict:
    data = services.registry(site.api_site, request)
    models = []
    for app in data["apps"]:
        for entry in app["models"]:
            path = ModelPath.parse(entry.get("path") or f"{app['app_label']}.{entry['model']}")
            chain = site.api_site.get_admin_chain(path)
            if chain is None or not all(site.is_exposed(a) for a in chain):
                continue
            item = {
                "model": path.label,
                "name": entry["name"],
                "name_plural": entry["name_plural"],
                "app": app["name"],
                "perms": _effective_perms(site, entry["perms"]),
            }
            if path.depth:
                item["nested_under"] = entry["nested_under"]
            description = site.get_model_description(chain[-1], request)
            if description:
                item["description"] = description
            if chain[-1].has_view_or_change_permission(request):
                views = _views(site, request, chain[-1])
                if views:
                    item["views"] = [_view_summary(spec) for spec in views]
            models.append(item)
    return {"models": models}


def describe_model(site, request, args) -> dict:
    from ..jsonschema import input_schemas

    model_admin, label = _model_admin(site, request, args)
    pk = _arg(args, "pk", (int, str), required=False)
    instance = None
    if pk is not None:
        instance = services.get_object(model_admin, request, pk)
        if not model_admin.has_view_or_change_permission(request, instance):
            raise PermissionDeniedError()

    schema = services.schema(site.api_site, model_admin, _with_query(request, QueryDict(mutable=True)))
    out = {
        "model": label,
        "name": schema["model"]["name"],
        "name_plural": schema["model"]["name_plural"],
        "pk_field": schema["model"]["pk"],
        "perms": _effective_perms(site, schema["perms"]),
        "columns": [
            {k: c[k] for k in ("name", "label", "type", "sortable", "choices") if k in c} for c in schema["columns"]
        ],
        "filters": [
            {
                "title": f["title"],
                "parameters": f["parameters"],
                "choices": [{"display": c["display"], "params": c["params"]} for c in f["choices"]],
            }
            for f in schema["filters"]
        ],
        "search": {"enabled": schema["search"]["enabled"], "help_text": schema["search"]["help_text"]},
        "default_ordering": schema["ordering"]["default"],
        "date_hierarchy": schema["date_hierarchy"],
        "per_page": {"default": site.default_per_page, "max": site.api_site.max_page_size},
    }
    if "parents" in schema["model"]:
        out["parents"] = schema["model"]["parents"]
    children = _children(site, request, model_admin)
    if children:
        out["children"] = [{k: c[k] for k in ("model", "name", "name_plural")} for c in children]
    description = site.get_model_description(model_admin, request)
    if description:
        out["description"] = description
    if site.read_only:
        out["actions"] = []
        out["input_schema"] = {"create": None, "update": None}
    else:
        out["actions"] = schema["actions"]
        out["input_schema"] = input_schemas(model_admin, request, instance)
    views = _views(site, request, model_admin)
    if instance is not None:
        views = [
            v for v in views if not v.detail or custom_views.has_view_permission(model_admin, request, v, instance)
        ]
    out["views"] = [_describe_view(spec) for spec in views]
    return out


def list_objects(site, request, args) -> dict:
    model_admin, label = _model_admin(site, request, args)
    query = _filters_query(args)
    search = _arg(args, "search", str, required=False)
    if search:
        query[SEARCH_VAR] = search
    ordering = _arg(args, "ordering", (list, str), required=False)
    if ordering:
        query[ORDER_VAR] = _ordering_param(model_admin, request, ordering)
    page = _arg(args, "page", int, required=False)
    if page is not None:
        query[PAGE_VAR] = str(page)
    per_page = _arg(args, "per_page", int, required=False, default=site.default_per_page)
    query[PER_PAGE_PARAM] = str(per_page)
    query[SHAPE_PARAM] = "columnar"
    data = services.changelist(site.api_site, model_admin, _with_query(request, query))
    data.pop("next", None)
    data.pop("previous", None)
    head = {"model": label}
    if request.admin_api_target.parents:
        head["parents"] = request.admin_api_target.parents
    return {**head, **data}


def get_object(site, request, args) -> dict:
    model_admin, _label = _model_admin(site, request, args)
    data = services.detail(model_admin, request, _arg(args, "pk", (int, str)))
    data.pop("children", None)  # REST urls; MCP addresses children by model path + parents
    children = _children(site, request, model_admin, data["pk"])
    if children:
        data["children"] = {c["segment"]: {"model": c["model"], "parents": c["parents"]} for c in children}
    # REST gives urls; MCP gives the names of the detail views (run them with run_view)
    declared = custom_views.declared_views(model_admin)
    views = [
        name
        for name in data.pop("views", {})
        if declared[name].mcp_enabled
        and not (site.read_only and declared[name].writes)
        and site.is_view_exposed(model_admin, declared[name])
    ]
    if views:
        data["views"] = views
    return data


def create_object(site, request, args) -> dict:
    _ensure_writable(site)
    model_admin, _label = _model_admin(site, request, args)
    data = _arg(args, "data", dict)
    if _arg(args, "dry_run", bool, required=False, default=False):
        return _dry_run(model_admin, request, lambda: services.create(model_admin, request, data))
    return _write_result(services.create(model_admin, request, data))


def update_object(site, request, args) -> dict:
    _ensure_writable(site)
    model_admin, _label = _model_admin(site, request, args)
    pk = _arg(args, "pk", (int, str))
    data = _arg(args, "data", dict)

    def run():
        return services.update(model_admin, request, pk, data, partial=True)

    if _arg(args, "dry_run", bool, required=False, default=False):
        return _dry_run(model_admin, request, run)
    return _write_result(run())


def delete_object(site, request, args) -> dict:
    _ensure_writable(site)
    model_admin, _label = _model_admin(site, request, args)
    pk = _arg(args, "pk", (int, str))
    if not _arg(args, "confirm", bool, required=False, default=False):
        preview = services.delete_preview(model_admin, request, pk)
        return {
            "deleted": False,
            "confirm_required": True,
            "preview": preview,
            "note": "Nothing was deleted. Call again with confirm=true to delete.",
        }
    return {"deleted": True, "object": services.delete(model_admin, request, pk)["deleted"]}


def run_action(site, request, args) -> dict:
    _ensure_writable(site)
    model_admin, _label = _model_admin(site, request, args)
    action = _arg(args, "action", str)
    payload: dict = {}
    if _arg(args, "all", bool, required=False, default=False):
        payload["all"] = True
    else:
        payload["pks"] = _arg(args, "pks", list, required=False, default=[])
    return services.run_action(model_admin, _with_query(request, _filters_query(args)), action, payload)


def run_view(site, request, args) -> dict:
    model_admin, _label = _model_admin(site, request, args)
    return execute_view(
        site,
        request,
        model_admin,
        _arg(args, "view", str),
        pk=_arg(args, "pk", (int, str), required=False),
        params=_arg(args, "params", dict, required=False, default={}),
        dry_run=_arg(args, "dry_run", bool, required=False, default=False),
    )


# --------------------------------------------------------------------------------------------
# catalogue
# --------------------------------------------------------------------------------------------
READ = {"readOnlyHint": True, "openWorldHint": False}

TOOLS = [
    ToolSpec(
        name="list_models",
        title="List models",
        description="List the data models you can access, with your permissions on each. Start here.",
        input_schema=obj({}),
        output_schema=ANY_OBJECT,
        handler=list_models,
        annotations=READ,
    ),
    ToolSpec(
        name="describe_model",
        title="Describe a model",
        description="Columns, filters, search, actions and the JSON Schema of the data accepted by "
        "create_object/update_object. Call this before listing with filters or writing. Pass pk to get the "
        "update schema for one object (some fields may be read-only per object).",
        input_schema=obj({"model": MODEL_ARG, "parents": PARENTS_ARG, "pk": PK_ARG}, ["model"]),
        output_schema=ANY_OBJECT,
        handler=describe_model,
        annotations=READ,
    ),
    ToolSpec(
        name="list_objects",
        title="List objects",
        description="List objects the way the admin changelist does: filters, search, ordering and paging. "
        "Returns columnar data: 'columns' names each position in every 'rows' entry.",
        input_schema=obj(
            {
                "model": MODEL_ARG,
                "parents": PARENTS_ARG,
                "filters": FILTERS_ARG,
                "search": {"type": "string", "description": "Free-text search (when describe_model says enabled)."},
                "ordering": {
                    "type": ["array", "string"],
                    "items": {"type": "string"},
                    "description": "Sortable column names; prefix '-' for descending, e.g. ['-date', 'slug'].",
                },
                "page": {"type": "integer", "minimum": 1, "default": 1},
                "per_page": {"type": "integer", "minimum": 1, "description": "Rows per page (default 25)."},
            },
            ["model"],
        ),
        output_schema=ANY_OBJECT,
        handler=list_objects,
        annotations=READ,
    ),
    ToolSpec(
        name="get_object",
        title="Get one object",
        description="All fields of one object, including its inline rows.",
        input_schema=obj({"model": MODEL_ARG, "parents": PARENTS_ARG, "pk": PK_ARG}, ["model", "pk"]),
        output_schema=ANY_OBJECT,
        handler=get_object,
        annotations=READ,
    ),
    ToolSpec(
        name="create_object",
        title="Create an object",
        description="Create an object through the admin's own form and save pipeline (same validation and "
        "business rules as the admin). 'data' must match describe_model's input_schema.create. "
        "Use dry_run=true first to check it.",
        input_schema=obj(
            {"model": MODEL_ARG, "parents": PARENTS_ARG, "data": DATA_ARG, "dry_run": DRY_RUN_ARG}, ["model", "data"]
        ),
        output_schema=ANY_OBJECT,
        handler=create_object,
        writes=True,
        annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    ),
    ToolSpec(
        name="update_object",
        title="Update an object",
        description="Change some fields of an object (fields not given are kept). Inline rows: a row with "
        "'pk' updates that row, '_delete': true deletes it, a row without 'pk' is added. 'data' must match "
        "describe_model's input_schema.update. Use dry_run=true first to check it.",
        input_schema=obj(
            {"model": MODEL_ARG, "parents": PARENTS_ARG, "pk": PK_ARG, "data": DATA_ARG, "dry_run": DRY_RUN_ARG},
            ["model", "pk", "data"],
        ),
        output_schema=ANY_OBJECT,
        handler=update_object,
        writes=True,
        annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
    ),
    ToolSpec(
        name="delete_object",
        title="Delete an object",
        description="Without confirm: returns what would be deleted (cascades, protected objects) and deletes "
        "nothing. With confirm=true: deletes the object.",
        input_schema=obj(
            {
                "model": MODEL_ARG,
                "parents": PARENTS_ARG,
                "pk": PK_ARG,
                "confirm": {"type": "boolean", "default": False, "description": "Actually delete."},
            },
            ["model", "pk"],
        ),
        output_schema=ANY_OBJECT,
        handler=delete_object,
        writes=True,
        annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
    ),
    ToolSpec(
        name="run_action",
        title="Run an admin action",
        description="Run one of the model's admin actions (listed by describe_model) on the given pks, or on "
        "all objects matching 'filters' with all=true. Actions have no dry run.",
        input_schema=obj(
            {
                "model": MODEL_ARG,
                "parents": PARENTS_ARG,
                "action": {"type": "string", "description": "Action name from describe_model's actions."},
                "pks": {"type": "array", "items": PK_ARG, "description": "Objects to run the action on."},
                "all": {"type": "boolean", "default": False, "description": "Run on all objects matching filters."},
                "filters": FILTERS_ARG,
            },
            ["model", "action"],
        ),
        output_schema=ANY_OBJECT,
        handler=run_action,
        writes=True,
        annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
    ),
    ToolSpec(
        name="run_view",
        title="Run a custom view",
        description="Run one of a model's custom views: reports, statements, computed pages or business "
        "operations. They are listed under 'views' by list_models (names) and describe_model (parameters as "
        "params_schema). Views with detail=true run on one object (give pk). Views with writes=true change "
        "data: use dry_run=true first to check them.",
        input_schema=obj(
            {
                "model": MODEL_ARG,
                "parents": PARENTS_ARG,
                "view": {"type": "string", "description": "View name from list_models / describe_model."},
                "pk": {**PK_ARG, "description": "The object, for views with detail=true."},
                "params": {"type": "object", "description": "Parameters matching the view's params_schema."},
                "dry_run": {
                    **DRY_RUN_ARG,
                    "description": "For views with writes=true: run the view, then roll everything back.",
                },
            },
            ["model", "view"],
        ),
        output_schema=ANY_OBJECT,
        handler=run_view,
        # stays on read-only sites, where write views are hidden and refused
        annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
    ),
]


def view_tool(site, model_admin, spec) -> ToolSpec:
    """A view published as its own tool (``@admin_api.view(mcp_tool="name")``). Its parameters
    are flattened next to ``pk`` (detail views) and ``dry_run`` (write views)."""
    opts = model_admin.model._meta
    label = f"{opts.app_label}.{opts.model_name}"
    params = spec.params_schema()
    properties = {}
    required = list(params.get("required", ()))
    if spec.detail:
        properties["pk"] = {**PK_ARG, "description": f"Primary key of the {opts.verbose_name}."}
        required.insert(0, "pk")
    properties.update(params["properties"])
    if spec.writes:
        properties["dry_run"] = DRY_RUN_ARG
    description = spec.description or spec.title
    description += f" (Custom view {spec.name!r} of {label}; same as run_view.)"

    def handler(site, request, args):
        model_admin = services.get_model_admin_by_label(site.api_site, request, label)
        if not site.is_exposed(model_admin):
            raise NotFound(f"No MCP access to {label}.")
        params = {k: v for k, v in args.items() if k not in custom_views.TOOL_RESERVED}
        return execute_view(
            site,
            request,
            model_admin,
            spec.name,
            pk=args.get("pk") if spec.detail else None,
            params=params,
            dry_run=bool(args.get("dry_run")) if spec.writes else False,
        )

    annotations = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False} if spec.writes else READ
    return ToolSpec(
        name=spec.mcp_tool,
        title=spec.title,
        description=description,
        input_schema=obj(properties, required),
        output_schema=ANY_OBJECT,
        handler=handler,
        writes=spec.writes,
        annotations=annotations,
    )
