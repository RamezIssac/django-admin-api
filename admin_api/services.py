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
from .errors import ApiError, NotFound, PermissionDeniedError
from .resolver import resolve


# --------------------------------------------------------------------------------------------
# lookup
# --------------------------------------------------------------------------------------------
def get_model_admin(api_site, request, app_label: str, model_name: str):
    """The API admin for ``app_label.model_name``, if exposed and viewable by ``request.user``."""
    model_admin = api_site.get_model_admin(app_label, model_name)
    if model_admin is None:
        raise NotFound(f"No API for {app_label}.{model_name}.")
    if not model_admin.has_view_or_change_permission(request):
        raise PermissionDeniedError()
    # Second line of defense: also enforced in ApiAdminMixin.has_*_permission, but an admin may
    # override those without calling super().
    scopes.check(request, model_admin.opts, "view")
    return model_admin


def get_model_admin_by_label(api_site, request, label: str):
    """Same as :func:`get_model_admin` for a ``"app_label.model_name"`` string."""
    app_label, sep, model_name = (label or "").strip().lower().partition(".")
    if not sep or not app_label or not model_name:
        raise ApiError(f"Expected 'app_label.model_name', got {label!r}.", code="bad_model")
    return get_model_admin(api_site, request, app_label, model_name)


def get_object(model_admin, request, object_id):
    obj = model_admin.get_object(request, unquote(str(object_id)))
    if obj is None:
        raise NotFound(f"{model_admin.model._meta.verbose_name} with pk {object_id!r} does not exist.")
    return obj


# --------------------------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------------------------
def registry(api_site, request) -> dict:
    """The apps/models the current user may use, with their permissions."""
    try:
        base = reverse(f"{api_site.name}:index")
    except NoReverseMatch:  # API urls not mounted (e.g. MCP only)
        base = None
    app_dict: dict[str, dict] = {}
    for model, model_admin in api_site.exposed_admins():
        if not model_admin.has_module_permission(request):
            continue
        perms = model_admin.get_model_perms(request)
        if not any(perms.values()):
            continue
        opts = model._meta
        app = app_dict.setdefault(
            opts.app_label,
            {
                "app_label": opts.app_label,
                "name": str(apps.get_app_config(opts.app_label).verbose_name),
                "models": [],
            },
        )
        app["models"].append(
            {
                "model": opts.model_name,
                "object_name": opts.object_name,
                "name": str(opts.verbose_name),
                "name_plural": str(opts.verbose_name_plural),
                "perms": perms,
                "url": f"{base}{opts.app_label}/{opts.model_name}/" if base else None,
            }
        )
    app_list = sorted(app_dict.values(), key=lambda a: a["name"].lower())
    for app in app_list:
        app["models"].sort(key=lambda m: m["name"].lower())
    return {"version": api_site.version, "apps": app_list}


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
    from .writes import describe_form

    split_api_params(request)
    columns = get_columns(model_admin, request)
    cl = build_changelist(model_admin, request, columns, schema_only=True)
    opts = model_admin.model._meta
    search_fields = list(model_admin.get_search_fields(request) or ())
    return {
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
        "actions": describe_actions(model_admin, request),
    }


def detail(model_admin, request, object_id) -> dict:
    from .detail import serialize_object

    obj = get_object(model_admin, request, object_id)
    if not model_admin.has_view_or_change_permission(request, obj):
        raise PermissionDeniedError()
    return serialize_object(model_admin, request, obj)


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
