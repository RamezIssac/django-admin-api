"""The MCP tools: thin adapters over :mod:`admin_api.services`.

Each handler is ``handler(mcp_site, request, arguments) -> dict``. They run synchronously in
a Django request context and raise :class:`admin_api.errors.ApiError` on failure.
"""

from __future__ import annotations

import copy

from django.contrib.admin.views.main import ORDER_VAR, PAGE_VAR, SEARCH_VAR
from django.db import router, transaction
from django.http import QueryDict
from django.utils.translation import gettext, gettext_noop

from .. import custom_views, memory, services
from ..changelist import PER_PAGE_PARAM, SHAPE_PARAM, describe_columns, get_columns
from ..errors import InvalidArguments, NotFound, PermissionDeniedError
from ..nested import ModelPath
from ..writes import ValidationFailed
from . import confirm
from .resolve import MAX_LIMIT, MAX_TERMS, resolve_entities
from .tools import (
    ANY_OBJECT,
    CONFIRM_TOKEN_ARG,
    DATA_ARG,
    DRY_RUN_ARG,
    FILTERS_ARG,
    ID_ARG,
    MODEL_ARG,
    PARENTS_ARG,
    PK_ALIAS_ARG,
    ToolSpec,
    obj,
)

RESERVED_FILTER_KEYS = frozenset({SEARCH_VAR, ORDER_VAR, PAGE_VAR, SHAPE_PARAM, PER_PAGE_PARAM, "all"})
NOT_AVAILABLE = gettext_noop("This kind of record is not available here.")


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


def _id_arg(args: dict, *, required=True):
    """The record ID argument: ``id``, or its deprecated alias ``pk``."""
    value = args.get("id", args.get("pk"))
    if value is None:
        if required:
            raise InvalidArguments("'id' is required.")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise InvalidArguments("'id' has the wrong type.")
    return value


def _ids_arg(args: dict) -> list:
    """The record IDs argument: ``ids``, or its deprecated alias ``pks``."""
    value = args.get("ids", args.get("pks"))
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidArguments("'ids' has the wrong type.")
    return value


def mcp_keys(value):
    """REST keys to MCP keys: a record reference ``{"pk", "str"}`` becomes ``{"id", "label"}``, anywhere."""
    if isinstance(value, list):
        return [mcp_keys(v) for v in value]
    if isinstance(value, dict):
        if value.keys() == {"pk", "str"}:
            return {"id": value["pk"], "label": value["str"]}
        return {k: mcp_keys(v) for k, v in value.items()}
    return value


def _object_keys(data: dict) -> dict:
    """A serialized object for MCP: ``id`` and ``label`` first, inline rows keyed by ``id``."""
    out = {"id": data.pop("pk")}
    if "str" in data:
        out["label"] = data.pop("str")
    out.update(mcp_keys(data))
    for prefix, rows in out.get("inlines", {}).items():
        out["inlines"][prefix] = [{"id": row.pop("pk"), **row} if "pk" in row else row for row in rows]
    return out


def _input_row(row):
    if not isinstance(row, dict) or "id" not in row or "pk" in row:
        return row
    row = dict(row)
    row["pk"] = row.pop("id")
    return row


def _input_data(data: dict) -> dict:
    """MCP write data for the services: an inline row's ``id`` is the row's ``pk``."""
    inlines = data.get("inlines")
    if not isinstance(inlines, dict):
        return data
    rows = {prefix: [_input_row(r) for r in rs] if isinstance(rs, list) else rs for prefix, rs in inlines.items()}
    return {**data, "inlines": rows}


def _model_admin(site, request, args):
    """The admin for ``model`` (a model path) inside ``parents``. Hiding an admin from MCP hides
    the admins nested under it too."""
    path = ModelPath.parse(_arg(args, "model", str))
    parents = _arg(args, "parents", list, required=False, default=[])
    if any(not isinstance(pk, (int, str)) or isinstance(pk, bool) for pk in parents):
        raise InvalidArguments("'parents' must be a list of IDs.")
    chain = site.api_site.get_admin_chain(path)
    if chain is not None and not all(site.is_exposed(a) for a in chain):
        raise NotFound(gettext(NOT_AVAILABLE))
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
    totals = data.pop("totals", None)
    out = {"object": _object_keys(data)}
    if totals:
        out["totals"] = totals
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
                out = {"dry_run": True, "valid": False, "errors": mcp_keys(exc.extra)}
            transaction.set_rollback(True)
    finally:
        request.is_dry_run = False
    if out["valid"]:
        out["note"] = gettext("Nothing was saved; the ID shown is provisional.")
    return out


def _write_scope(request, **arguments) -> dict:
    """What a confirm token covers: the target model and parents plus the write's own arguments."""
    target = request.admin_api_target
    return {"model": target.path.label, "parents": target.parents, **arguments}


def _issue_confirm_token(site, request, out: dict, operation: str, scope: dict, state) -> dict:
    out["confirm_token"] = confirm.issue(site, request, operation, scope, state)
    out["note"] = gettext(
        "Nothing was saved; the ID shown is provisional. To save, repeat the call without dry_run "
        "and pass this confirm_token."
    )
    return out


def _confirmed_write(site, request, model_admin, token, operation: str, scope: dict, obj, run):
    """A gated real write: lock the object's row, spend ``token`` and write in one transaction. A
    failed write rolls the token back to unspent."""
    with transaction.atomic(using=router.db_for_write(model_admin.model)):
        confirm.verify(site, token, request, operation, scope, confirm.object_state(obj, lock=True))
        return run()


def _guarded_write(site, request, model_admin, args, run, operation: str, scope: dict, obj=None) -> dict:
    """A create/update of ``obj`` (``None`` for a create), its permissions already checked: with
    ``require_dry_run``, a valid dry run issues the ``confirm_token`` that the real write must present."""
    gated = site.requires_dry_run(model_admin)
    if _arg(args, "dry_run", bool, required=False, default=False):
        state = confirm.object_state(obj) if gated else None
        out = _dry_run(model_admin, request, run)
        return _issue_confirm_token(site, request, out, operation, scope, state) if gated and out["valid"] else out
    if gated:
        token = args.get("confirm_token")
        return _write_result(_confirmed_write(site, request, model_admin, token, operation, scope, obj, run))
    return _write_result(run())


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


def execute_view(site, request, model_admin, name, *, pk=None, params=None, dry_run=False, confirm_token=None) -> dict:
    """Run a custom view for MCP (``run_view`` and the promoted view tools)."""
    spec = custom_views.get_view(model_admin, request, name, mcp=True, read_only=site.read_only)
    if not site.is_view_exposed(model_admin, spec):
        raise NotFound(gettext("No view '%(view)s'.") % {"view": name})
    if params is not None and not isinstance(params, dict):
        raise InvalidArguments("'params' must be an object.")

    # 404 and 403 come before the confirm token's 409
    obj = custom_views.view_object(model_admin, request, spec, pk)
    custom_views.check_view_permission(model_admin, request, spec, obj)

    def run():
        out = mcp_keys(services.run_view(model_admin, request, name, object_id=pk, data=params or {}, obj=obj))
        if "pk" in out:
            out["id"] = out.pop("pk")
        return out

    gated = spec.writes and site.requires_dry_run(model_admin)
    scope = _write_scope(request, view=name, pk=None if pk is None else str(pk), params=params or {})
    if not dry_run:
        if gated:
            return _confirmed_write(site, request, model_admin, confirm_token, "run_view", scope, obj, run)
        return run()
    if not spec.writes:
        return {**run(), "dry_run": True, "note": gettext("A read-only view; nothing to roll back.")}
    state = confirm.object_state(obj) if gated else None
    request.is_dry_run = True
    try:
        with transaction.atomic(using=router.db_for_write(model_admin.model)):
            out = {**run(), "dry_run": True, "note": gettext("Nothing was saved: the view ran and was rolled back.")}
            transaction.set_rollback(True)
    finally:
        request.is_dry_run = False
    return _issue_confirm_token(site, request, out, "run_view", scope, state) if gated else out


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
                "name": entry["name"],
                "name_plural": entry["name_plural"],
                "model": path.label,
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
    pk = _id_arg(args, required=False)
    instance = None
    if pk is not None:
        instance = services.get_object(model_admin, request, pk)
        if not model_admin.has_view_or_change_permission(request, instance):
            raise PermissionDeniedError(
                gettext("You may not view this %(model)s.") % {"model": model_admin.opts.verbose_name}
            )

    schema = services.schema(site.api_site, model_admin, _with_query(request, QueryDict(mutable=True)))
    out = {
        "name": schema["model"]["name"],
        "name_plural": schema["model"]["name_plural"],
        "model": label,
        "id_field": schema["model"]["pk"],
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
    data["columns"] = ["id", *data["columns"][1:]]
    data["rows"] = mcp_keys(data["rows"])
    return {**head, **data}


def get_object(site, request, args) -> dict:
    model_admin, _label = _model_admin(site, request, args)
    data = _object_keys(services.detail(model_admin, request, _id_arg(args)))
    data.pop("children", None)  # REST urls; MCP addresses children by model path + parents
    children = _children(site, request, model_admin, data["id"])
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
    data = _input_data(_arg(args, "data", dict))
    services.check_add_permission(model_admin, request)
    return _guarded_write(
        site,
        request,
        model_admin,
        args,
        lambda: services.create(model_admin, request, data),
        "create_object",
        _write_scope(request, data=data),
    )


def update_object(site, request, args) -> dict:
    _ensure_writable(site)
    model_admin, _label = _model_admin(site, request, args)
    pk = _id_arg(args)
    data = _input_data(_arg(args, "data", dict))
    obj = services.get_object(model_admin, request, pk)
    services.check_change_permission(model_admin, request, obj)
    return _guarded_write(
        site,
        request,
        model_admin,
        args,
        lambda: services.update(model_admin, request, pk, data, partial=True, obj=obj),
        "update_object",
        _write_scope(request, pk=str(pk), data=data),
        obj,
    )


def delete_object(site, request, args) -> dict:
    _ensure_writable(site)
    model_admin, _label = _model_admin(site, request, args)
    pk = _id_arg(args)
    if not _arg(args, "confirm", bool, required=False, default=False):
        preview = services.delete_preview(model_admin, request, pk)
        return {
            "deleted": False,
            "confirm_required": True,
            "preview": mcp_keys(preview),
            "note": gettext("Nothing was deleted. Call again with confirm=true to delete."),
        }
    return {"deleted": True, "object": mcp_keys(services.delete(model_admin, request, pk)["deleted"])}


def run_action(site, request, args) -> dict:
    _ensure_writable(site)
    model_admin, _label = _model_admin(site, request, args)
    action = _arg(args, "action", str)
    payload: dict = {}
    if _arg(args, "all", bool, required=False, default=False):
        payload["all"] = True
    else:
        payload["pks"] = _ids_arg(args)
    return mcp_keys(services.run_action(model_admin, _with_query(request, _filters_query(args)), action, payload))


def run_view(site, request, args) -> dict:
    model_admin, _label = _model_admin(site, request, args)
    return execute_view(
        site,
        request,
        model_admin,
        _arg(args, "view", str),
        pk=_id_arg(args, required=False),
        params=_arg(args, "params", dict, required=False, default={}),
        dry_run=_arg(args, "dry_run", bool, required=False, default=False),
        confirm_token=args.get("confirm_token"),
    )


def set_preference(site, request, args):
    key = _arg(args, "key", str)
    request.mcp_memory = memory.set_preference(request.user, key, args.get("value"))
    return {
        "preferences": request.mcp_memory.as_dict(),
        "note": gettext("Language applies from the next tool call; audience and notes from the next connection."),
    }


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
        "create_object/update_object. Call this before listing with filters or writing. Pass id to get the "
        "update schema for one record (some fields may be read-only per record).",
        input_schema=obj({"model": MODEL_ARG, "parents": PARENTS_ARG, "id": ID_ARG, "pk": PK_ALIAS_ARG}, ["model"]),
        output_schema=ANY_OBJECT,
        handler=describe_model,
        annotations=READ,
    ),
    ToolSpec(
        name="list_objects",
        title="List objects",
        description="List objects the way the admin changelist does: filters, search, ordering and paging. "
        "Returns columnar data: 'columns' names each position in every 'rows' entry; the first is the record 'id', "
        "and a related record is {'id', 'label'}.",
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
        name="resolve_entities",
        title="Resolve names to ids",
        description="Turn names the user typed into object ids and models, across every model you can see. "
        "Each term returns 'objects' (records whose search fields match) and 'models' (models whose name "
        "matches, e.g. 'invoices'), each with status 'unique', 'ambiguous' or 'none' and ranked candidates. "
        "Use it before filtering, reporting or writing with a name.",
        input_schema=obj(
            {
                "terms": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_TERMS,
                    "items": obj(
                        {
                            "text": {"type": "string", "description": "The user's words, unchanged."},
                            "kind": {
                                "enum": ["any", "objects", "models"],
                                "default": "any",
                                "description": "Search objects, models or both.",
                            },
                            "models": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Only these models (e.g. a parameter's x-related-model).",
                            },
                            "for_field": obj(
                                {
                                    "model": {"type": "string", "description": "The model being written."},
                                    "field": {
                                        "type": "string",
                                        "description": "A relation field of its input_schema, or "
                                        "'inlines.<prefix>.<field>' for an inline row field.",
                                    },
                                },
                                ["model", "field"],
                                description="Resolve a value for this create/update field: searches the "
                                "related model, with the field's own choice limits.",
                            ),
                        },
                        ["text"],
                    ),
                },
                "models": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Default 'models' for every term without its own 'models' or 'for_field'.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 5},
            },
            ["terms"],
        ),
        output_schema=ANY_OBJECT,
        handler=resolve_entities,
        annotations=READ,
    ),
    ToolSpec(
        name="get_object",
        title="Get one object",
        description="All fields of one record ('id', 'label', then the fields), including its inline rows.",
        input_schema=obj({"model": MODEL_ARG, "parents": PARENTS_ARG, "id": ID_ARG, "pk": PK_ALIAS_ARG}, ["model"]),
        output_schema=ANY_OBJECT,
        handler=get_object,
        annotations=READ,
    ),
    ToolSpec(
        name="create_object",
        title="Create an object",
        description="Create an object through the admin's own form and save pipeline (same validation and "
        "business rules as the admin). 'data' must match describe_model's input_schema.create. "
        "Use dry_run=true first to check it. The result carries the document's computed 'totals' when the "
        "admin declares them.",
        input_schema=obj(
            {
                "model": MODEL_ARG,
                "parents": PARENTS_ARG,
                "data": DATA_ARG,
                "dry_run": DRY_RUN_ARG,
                "confirm_token": CONFIRM_TOKEN_ARG,
            },
            ["model", "data"],
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
        "'id' updates that row, '_delete': true deletes it, a row without 'id' is added. 'data' must match "
        "describe_model's input_schema.update. Use dry_run=true first to check it. The result carries the "
        "document's computed 'totals' when the admin declares them.",
        input_schema=obj(
            {
                "model": MODEL_ARG,
                "parents": PARENTS_ARG,
                "id": ID_ARG,
                "pk": PK_ALIAS_ARG,
                "data": DATA_ARG,
                "dry_run": DRY_RUN_ARG,
                "confirm_token": CONFIRM_TOKEN_ARG,
            },
            ["model", "data"],
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
                "id": ID_ARG,
                "pk": PK_ALIAS_ARG,
                "confirm": {"type": "boolean", "default": False, "description": "Actually delete."},
            },
            ["model"],
        ),
        output_schema=ANY_OBJECT,
        handler=delete_object,
        writes=True,
        annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
    ),
    ToolSpec(
        name="run_action",
        title="Run an admin action",
        description="Run one of the model's admin actions (listed by describe_model) on the given ids, or on "
        "all objects matching 'filters' with all=true. Actions have no dry run.",
        input_schema=obj(
            {
                "model": MODEL_ARG,
                "parents": PARENTS_ARG,
                "action": {"type": "string", "description": "Action name from describe_model's actions."},
                "ids": {"type": "array", "items": ID_ARG, "description": "IDs of the records to run the action on."},
                "pks": {"type": "array", "items": ID_ARG, "description": "Deprecated alias of 'ids'."},
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
        "params_schema). Views with detail=true run on one record (give id). Views with writes=true change "
        "data: use dry_run=true first to check them.",
        input_schema=obj(
            {
                "model": MODEL_ARG,
                "parents": PARENTS_ARG,
                "view": {"type": "string", "description": "View name from list_models / describe_model."},
                "id": {**ID_ARG, "description": "The record, for views with detail=true."},
                "pk": PK_ALIAS_ARG,
                "params": {"type": "object", "description": "Parameters matching the view's params_schema."},
                "dry_run": {
                    **DRY_RUN_ARG,
                    "description": "For views with writes=true: run the view, then roll everything back.",
                },
                "confirm_token": CONFIRM_TOKEN_ARG,
            },
            ["model", "view"],
        ),
        output_schema=ANY_OBJECT,
        handler=run_view,
        # stays on read-only sites, where write views are hidden and refused
        annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
    ),
    ToolSpec(
        name="set_preference",
        title="Remember a preference",
        description="Remember one of the user's own preferences across conversations, when the user asks for it. "
        "'audience': 'business' (default: plain business wording) or 'developer' (model paths and field names "
        "allowed). 'language': a language code (e.g. 'en', 'ar') that answers and record names come in. 'notes': "
        "up to 500 characters the user wants you to know. Send an empty value to clear a preference.",
        input_schema=obj(
            {
                "key": {"type": "string", "enum": list(memory.PREFERENCE_KEYS)},
                "value": {"type": ["string", "null"], "description": "The new value; empty or null clears it."},
            },
            ["key"],
        ),
        output_schema=ANY_OBJECT,
        handler=set_preference,
        # the user's own preferences, not business data: listed on read-only sites too
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
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
    # a view param named 'id' keeps its name; the record ID then goes in 'pk'
    id_name = "id" if "id" not in params["properties"] else "pk"
    if spec.detail:
        properties[id_name] = {**ID_ARG, "description": gettext("ID of the %(model)s.") % {"model": opts.verbose_name}}
        if id_name == "id":
            properties["pk"] = PK_ALIAS_ARG
    properties.update(params["properties"])
    if spec.writes:
        properties["dry_run"] = DRY_RUN_ARG
        properties["confirm_token"] = CONFIRM_TOKEN_ARG
    description = str(spec.description or spec.title)
    description += " " + gettext("(Same as run_view '%(view)s' on %(models)s.)") % {
        "view": spec.name,
        "models": opts.verbose_name_plural,
    }
    reserved = custom_views.TOOL_RESERVED | ({"id"} if spec.detail and id_name == "id" else set())

    def handler(site, request, args):
        model_admin = services.get_model_admin_by_label(site.api_site, request, label)
        if not site.is_exposed(model_admin):
            raise NotFound(gettext(NOT_AVAILABLE))
        params = {k: v for k, v in args.items() if k not in reserved}
        pk = (args.get("id", args.get("pk")) if id_name == "id" else args.get("pk")) if spec.detail else None
        if spec.detail and pk is None:
            raise InvalidArguments(f"'{id_name}' is required.")
        return execute_view(
            site,
            request,
            model_admin,
            spec.name,
            pk=pk,
            params=params,
            dry_run=bool(args.get("dry_run")) if spec.writes else False,
            confirm_token=args.get("confirm_token"),
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
