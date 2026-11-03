from __future__ import annotations

from django.http import JsonResponse
from django.views import View

from ..changelist import (
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
from ..encoders import encode_value
from ..errors import NotFound, PermissionDeniedError
from ..resolver import resolve


class ModelApiView(View):
    api_site = None

    def get_model_admin(self, request, app_label, model_name):
        model_admin = self.api_site.get_model_admin(app_label, model_name)
        if model_admin is None:
            raise NotFound(f"No API for {app_label}.{model_name}.")
        if not model_admin.has_view_or_change_permission(request):
            raise PermissionDeniedError()
        return model_admin

    def get_object(self, model_admin, request, object_id):
        from django.contrib.admin.utils import unquote

        obj = model_admin.get_object(request, unquote(object_id))
        if obj is None:
            raise NotFound(f"{model_admin.model._meta.verbose_name} with pk {object_id!r} does not exist.")
        return obj


class ChangeListView(ModelApiView):
    """``GET /v1/<app>/<model>/`` — Django's changelist as JSON."""

    def get(self, request, app_label, model_name):
        model_admin = self.get_model_admin(request, app_label, model_name)
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
            if not 1 <= per_page <= self.api_site.max_page_size:
                raise BadLookup(f"{PER_PAGE_PARAM} must be between 1 and {self.api_site.max_page_size}.")

        columns = get_columns(model_admin, request)
        cl = build_changelist(model_admin, request, columns, per_page=per_page)

        pk_name = model_admin.model._meta.pk.attname
        rows = [
            [encode_value(getattr(obj, pk_name))] + [c.value(obj, model_admin) for c in columns]
            for obj in cl.result_list
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
            "next": self._page_url(request, page + 1) if page < pages else None,
            "previous": self._page_url(request, page - 1) if page > 1 else None,
        }
        if shape == "columnar":
            payload["columns"] = keys
            payload["rows"] = rows
        else:
            payload["results"] = [dict(zip(keys, row)) for row in rows]
        return JsonResponse(payload)

    @staticmethod
    def _page_url(request, page):
        from django.contrib.admin.views.main import PAGE_VAR

        query = request.api_query.copy()
        query[PAGE_VAR] = page
        return f"{request.path}?{query.urlencode()}"


class SchemaView(ModelApiView):
    """``GET /v1/<app>/<model>/schema/`` — what the list endpoint accepts and returns."""

    def get(self, request, app_label, model_name):
        from django.contrib.admin.views.main import ALL_VAR, ORDER_VAR, PAGE_VAR, SEARCH_VAR

        model_admin = self.get_model_admin(request, app_label, model_name)
        split_api_params(request)
        columns = get_columns(model_admin, request)
        cl = build_changelist(model_admin, request, columns, schema_only=True)
        opts = model_admin.model._meta
        search_fields = list(model_admin.get_search_fields(request) or ())
        return JsonResponse(
            {
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
                    "max_per_page": self.api_site.max_page_size,
                    "show_all_param": ALL_VAR,
                    "max_show_all": model_admin.list_max_show_all,
                },
                "shapes": {"param": SHAPE_PARAM, "values": list(SHAPES), "default": "objects"},
            }
        )
