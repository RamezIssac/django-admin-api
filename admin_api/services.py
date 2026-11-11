"""Transport-neutral operations behind the API.

Every function takes a Django ``request`` (with ``user``, ``is_api`` and, for list-style
operations, the changelist query in ``request.GET``) and returns a JSON-serializable ``dict``,
or raises :class:`admin_api.errors.ApiError`. The REST views and the MCP tools are thin
adapters over these functions, so both surfaces share permissions, validation and output.
"""

from __future__ import annotations

from django.apps import apps
from django.contrib.admin.utils import unquote
from django.urls import NoReverseMatch, reverse

from . import scopes
from .changelist import (
    PER_PAGE_PARAM,
    SHAPE_PARAM,
    SHAPES,
    BadLookup,
    build_changelist,
    describe_columns,
    describe_filters,
    get_columns,
    split_api_params,
)
from .encoders import encode_value
from .errors import NotFound, PermissionDeniedError
from .nested import BadParents, ModelPath, Target
from .resolver import resolve


# --------------------------------------------------------------------------------------------
# lookup
# --------------------------------------------------------------------------------------------
def get_model_admin(api_site, request, app_label: str, model_name: str, *, segments=(), parents=()):
    """The API admin for ``app_label.model_name`` (or the admin nested under it at ``segments``,
    inside the objects ``parents``), if exposed and viewable by ``request.user``.

    Sets ``request.admin_api_target`` (:class:`admin_api.nested.Target`). For a nested admin,
    also prepares ``request`` for it (e.g. ``request.subadmin``): every ancestor is loaded through
    its admin and must be viewable.
    """
    path = ModelPath(app_label, model_name, tuple(segments))
    chain = api_site.get_admin_chain(path)
    if chain is None:
        raise NotFound(f"No API for {path.label}.")
    model_admin = chain[-1]
    parents = list(parents or ())
    if len(parents) != path.depth:
        raise BadParents(
            f"{path.label} needs {path.depth} parent pk(s), one per level, got {len(parents)}."
            if path.depth
            else f"{path.label} is not nested: no parents expected."
        )
    if path.depth:
        api_site.prepare_nested_request(model_admin, request, parents)
    if not model_admin.has_view_or_change_permission(request):
        raise PermissionDeniedError()
    # Second line of defense: also enforced in ApiAdminMixin.has_*_permission, but an admin may
    # override those without calling super().
    scopes.check(request, model_admin.opts, "view")
    request.admin_api_target = Target(api_site, path, parents, chain)
    return model_admin


def get_model_admin_by_label(api_site, request, label: str, parents=()):
    """Same as :func:`get_model_admin` for ``"app_label.model_name"`` or a nested path
    ``"app_label.model_name/child/grandchild"``."""
    path = ModelPath.parse(label)
    return get_model_admin(api_site, request, path.app_label, path.model_name, segments=path.segments, parents=parents)


def get_object(model_admin, request, object_id):
    obj = model_admin.get_object(request, unquote(str(object_id)))
    if obj is None:
        raise NotFound(f"{model_admin.model._meta.verbose_name} with pk {object_id!r} does not exist.")
    return obj


# --------------------------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------------------------
def _api_base(api_site):
    try:
        return reverse(f"{api_site.name}:index")
    except NoReverseMatch:  # API urls not mounted (e.g. MCP only)
        return None


def registry(api_site, request) -> dict:
    """The apps/models the current user may use, with their permissions.

    Nested admins are listed too, after a viewable parent, with ``path``, ``nested_under`` (the
    model of each parent pk) and ``url_template`` (``{0}``, ``{1}``… stand for the parent pks).
    """
    base = _api_base(api_site)
    app_dict: dict[str, dict] = {}

    def add(model_admin, path, ancestors):
        if not model_admin.has_module_permission(request):
            return
        perms = model_admin.get_model_perms(request)
        if not any(perms.values()):
            return
        opts = model_admin.model._meta
        app = app_dict.setdefault(
            opts.app_label,
            {
                "app_label": opts.app_label,
                "name": str(apps.get_app_config(opts.app_label).verbose_name),
                "models": [],
            },
        )
        entry = {
            "model": opts.model_name,
            "object_name": opts.object_name,
            "name": str(opts.verbose_name),
            "name_plural": str(opts.verbose_name_plural),
            "perms": perms,
            "url": path.url(base) if base and not path.depth else None,
        }
        if path.depth:
            entry["path"] = path.label
            entry["nested_under"] = list(ancestors)
            entry["url_template"] = path.url_template(base) if base else None
        app["models"].append(entry)
        # children need a viewable parent object
        if not model_admin.has_view_or_change_permission(request):
            return
        label = f"{opts.app_label}.{opts.model_name}"
        for segment, child in api_site.get_api_children(model_admin).items():
            add(child, path.child(segment), [*ancestors, label])

    for model, model_admin in api_site.exposed_admins():
        add(model_admin, ModelPath(model._meta.app_label, model._meta.model_name), [])
    app_list = sorted(app_dict.values(), key=lambda a: a["name"].lower())
    for app in app_list:
        app["models"].sort(key=lambda m: (m["name"].lower(), m.get("path", "")))
    return {"version": api_site.version, "apps": app_list}


def child_links(request, model_admin, pk=None) -> list[dict]:
    """The admins nested under ``model_admin`` that ``request.user`` may view.

    ``[{"segment", "model" (path), "admin", "name", "name_plural"}]``, plus ``"parents"`` and
    ``"url"`` (REST, ``None`` if not mounted) when the (encoded) ``pk`` of an object is given.
    No queries.
    """
    target = getattr(request, "admin_api_target", None)
    if target is None:
        return []
    base = _api_base(target.api_site) if pk is not None else None
    out = []
    for segment, child in target.api_site.get_api_children(model_admin).items():
        if not child.has_view_or_change_permission(request):  # includes token scopes
            continue
        path = target.path.child(segment)
        opts = child.model._meta
        link = {
            "segment": segment,
            "model": path.label,
            "admin": child,
            "name": str(opts.verbose_name),
            "name_plural": str(opts.verbose_name_plural),
        }
        if pk is not None:
            link["parents"] = [*target.parents, pk]
            link["url"] = path.url(base, link["parents"]) if base else None
        out.append(link)
    return out


def changelist(api_site, model_admin, request) -> dict:
    """Django's changelist as data. Filters, search, ordering and paging come from ``request.GET``
    in the admin's own format; ``_shape`` and ``_per_page`` are API-owned."""
    api_params = split_api_params(request)

    shape = api_params.get(SHAPE_PARAM) or "objects"
    if shape not in SHAPES:
        raise BadLookup(f"{SHAPE_PARAM} must be one of: {', '.join(SHAPES)}.")
    per_page = None
    if PER_PAGE_PARAM in api_params:
        try:
            per_page = int(api_params[PER_PAGE_PARAM])
        except (TypeError, ValueError):
            per_page = 0
        if not 1 <= per_page <= api_site.max_page_size:
            raise BadLookup(f"{PER_PAGE_PARAM} must be between 1 and {api_site.max_page_size}.")

    columns = get_columns(model_admin, request)
    cl = build_changelist(model_admin, request, columns, per_page=per_page)

    pk_name = model_admin.model._meta.pk.attname
    rows = [
        [encode_value(getattr(obj, pk_name))] + [c.value(obj, model_admin) for c in columns] for obj in cl.result_list
    ]
    keys = ["pk"] + [c.key for c in columns]

    showing_all = (cl.show_all and cl.can_show_all) or not cl.multi_page
    pages = 1 if showing_all else cl.paginator.num_pages
    page = 1 if showing_all else cl.page_num
    payload = {
        "count": cl.result_count,
        "page": page,
        "pages": pages,
        "per_page": cl.list_per_page,
        "next": _page_url(request, page + 1) if page < pages else None,
        "previous": _page_url(request, page - 1) if page > 1 else None,
    }
    if shape == "columnar":
        payload["columns"] = keys
        payload["rows"] = rows
    else:
        payload["results"] = [dict(zip(keys, row)) for row in rows]
    return payload


def _page_url(request, page):
    from django.contrib.admin.views.main import PAGE_VAR

    query = request.api_query.copy()
    query[PAGE_VAR] = page
    return f"{request.path}?{query.urlencode()}"


def schema(api_site, model_admin, request) -> dict:
    """What the list endpoint accepts and returns, plus form fields and actions."""
    from django.contrib.admin.views.main import ALL_VAR, ORDER_VAR, PAGE_VAR, SEARCH_VAR

    from .actions import describe_actions
    from .jsonschema import input_schemas
    from .writes import describe_form

    split_api_params(request)
    columns = get_columns(model_admin, request)
    cl = build_changelist(model_admin, request, columns, schema_only=True)
    opts = model_admin.model._meta
    search_fields = list(model_admin.get_search_fields(request) or ())
    payload = {
        "model": {
            "app_label": opts.app_label,
            "model": opts.model_name,
            "name": str(opts.verbose_name),
            "name_plural": str(opts.verbose_name_plural),
            "pk": opts.pk.attname,
        },
        "perms": model_admin.get_model_perms(request),
        "columns": describe_columns(columns, model_admin, request),
        "filters": describe_filters(cl),
        "search": {
            "enabled": bool(search_fields),
            "param": SEARCH_VAR,
            "help_text": str(model_admin.search_help_text or "") or None,
        },
        "ordering": {
            "param": ORDER_VAR,
            "format": "dot-separated order_index values, prefix '-' for descending, e.g. '2.-0'",
            "default": [str(o) for o in (model_admin.get_ordering(request) or ())],
        },
        "date_hierarchy": model_admin.date_hierarchy,
        "pagination": {
            "page_param": PAGE_VAR,
            "per_page": resolve(model_admin, "list_per_page", request),
            "per_page_param": PER_PAGE_PARAM,
            "max_per_page": api_site.max_page_size,
            "show_all_param": ALL_VAR,
            "max_show_all": model_admin.list_max_show_all,
        },
        "shapes": {"param": SHAPE_PARAM, "values": list(SHAPES), "default": "objects"},
        "form": describe_form(model_admin, request) if model_admin.has_add_permission(request) else None,
        "input_schema": input_schemas(model_admin, request),
        "actions": describe_actions(model_admin, request),
    }
    target = getattr(request, "admin_api_target", None)
    if target is not None and target.path.depth:
        payload["model"]["path"] = target.path.label
        payload["model"]["parents"] = list(target.parents)
    children = child_links(request, model_admin)
    if children:
        payload["children"] = [{k: link[k] for k in ("segment", "model", "name", "name_plural")} for link in children]
    return payload


def detail(model_admin, request, object_id) -> dict:
    from .detail import serialize_object

    obj = get_object(model_admin, request, object_id)
    if not model_admin.has_view_or_change_permission(request, obj):
        raise PermissionDeniedError()
    payload = serialize_object(model_admin, request, obj)
    children = child_links(request, model_admin, payload["pk"])
    if children:
        payload["children"] = {link["segment"]: link["url"] for link in children}
    return payload


# --------------------------------------------------------------------------------------------
# write
# --------------------------------------------------------------------------------------------
def _saved(model_admin, request, result, messages) -> dict:
    from .detail import serialize_object

    payload = serialize_object(model_admin, request, result.obj)
    if messages:
        payload["messages"] = messages
    return payload


def _load(payload):
    """Payloads may be passed lazily (a callable) so permission checks run before body parsing."""
    return payload() if callable(payload) else payload


def create(model_admin, request, payload) -> dict:
    from .writes import save_from_payload

    if not model_admin.has_add_permission(request):
        raise PermissionDeniedError()
    scopes.check(request, model_admin.opts, "add")
    result, messages = save_from_payload(model_admin, request, None, _load(payload), change=False, partial=False)
    return _saved(model_admin, request, result, messages)


def update(model_admin, request, object_id, payload, *, partial: bool) -> dict:
    from .writes import save_from_payload

    obj = get_object(model_admin, request, object_id)
    if not model_admin.has_change_permission(request, obj):
        raise PermissionDeniedError()
    scopes.check(request, model_admin.opts, "change")
    result, messages = save_from_payload(model_admin, request, obj, _load(payload), change=True, partial=partial)
    return _saved(model_admin, request, result, messages)


def delete(model_admin, request, object_id) -> dict:
    from .deletion import delete_object

    obj = get_object(model_admin, request, object_id)
    if not model_admin.has_delete_permission(request, obj):
        raise PermissionDeniedError()
    scopes.check(request, model_admin.opts, "delete")
    return {"deleted": delete_object(model_admin, request, obj)}


def delete_preview(model_admin, request, object_id) -> dict:
    from .deletion import delete_preview as _preview

    obj = get_object(model_admin, request, object_id)
    if not model_admin.has_delete_permission(request, obj):
        raise PermissionDeniedError()
    scopes.check(request, model_admin.opts, "delete")
    return _preview(model_admin, request, obj)


def run_action(model_admin, request, name: str, payload: dict) -> dict:
    """Run an admin action on ``{"pks": [...]}`` or ``{"all": true}``; with ``all``, the
    changelist filters in ``request.GET`` narrow the queryset."""
    from .actions import run_action as _run

    return _run(model_admin, request, name, payload)
