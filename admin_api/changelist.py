"""The API changelist: Django's ``ChangeList`` driven by the resolved ``api_`` twins."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.contrib.admin.utils import label_for_field, lookup_field
from django.contrib.admin.views.main import ChangeList
from django.core.exceptions import FieldDoesNotExist
from django.http import QueryDict

from .decorators import is_api_display
from .encoders import encode_value, field_type
from .errors import ApiError
from .resolver import api_value, resolve

# Query parameters owned by the API. Removed from request.GET before ChangeList sees it,
# because ChangeList treats every unknown key as a field lookup. Admin's own params
# (o, q, p, all, _facets, _to_field, ...) are left alone.
SHAPE_PARAM = "_shape"
PER_PAGE_PARAM = "_per_page"
API_PARAMS = frozenset({SHAPE_PARAM, PER_PAGE_PARAM})
SHAPES = ("objects", "columnar")


class BadLookup(ApiError):
    status = 400
    code = "bad_lookup"


@dataclass(frozen=True)
class Column:
    name: Any  # list_display entry (str or callable)
    key: str  # key in the JSON row
    kind: str  # "str" | "field" | "callable"
    field: Any = None

    def label(self, model_admin) -> str:
        if self.kind == "str":
            return str(model_admin.model._meta.verbose_name)
        return str(label_for_field(self.name, model_admin.model, model_admin))

    def value(self, obj, model_admin):
        if self.kind == "str":
            return str(obj)
        _f, _attr, value = lookup_field(self.name, obj, model_admin)
        return encode_value(value)


def _list_display_is_explicit(model_admin) -> bool:
    """True when list_display comes from an api_ twin (``get_api_list_display`` / ``api_list_display``)."""
    return (
        getattr(model_admin, "get_api_list_display", None) is not None
        or getattr(model_admin, "api_list_display", None) is not None
    )


def _model_field(model, name):
    if not isinstance(name, str):
        return None
    try:
        return model._meta.get_field(name)
    except FieldDoesNotExist:
        return None


def get_columns(model_admin, request) -> list[Column]:
    """Resolve list_display for the API.

    Model fields and ``__str__`` are always included. Anything else (admin methods, model
    methods/properties, plain callables) is a *callable* and is only included when the list
    comes from an api_ twin, or when it is decorated with ``@admin_api.display``.
    """
    explicit = _list_display_is_explicit(model_admin)
    model = model_admin.model
    columns = []
    for name in api_value(model_admin, "list_display", request):
        if name == "action_checkbox":
            continue
        if name == "__str__":
            columns.append(Column(name, "str", "str"))
            continue
        field = _model_field(model, name)
        if field is not None:
            columns.append(Column(name, name, "field", field))
            continue
        if isinstance(name, str) and "__" in name and not hasattr(model_admin, name) and not hasattr(model, name):
            # related lookup ("client__name"), supported by Django >= 5.1
            columns.append(Column(name, name, "field"))
            continue
        func = name if callable(name) else getattr(model_admin, name, None) or getattr(model, name, None)
        if explicit or is_api_display(func):
            key = name if isinstance(name, str) else getattr(name, "__name__", repr(name))
            columns.append(Column(name, key, "callable"))
    return columns


class ApiChangeList(ChangeList):
    """ChangeList that can skip fetching results (used by the schema endpoint)."""

    def __init__(self, *args, schema_only=False, **kwargs):
        self._schema_only = schema_only
        super().__init__(*args, **kwargs)

    def get_results(self, request):
        if self._schema_only:
            self.result_count = self.full_result_count = None
            self.result_list = []
            self.can_show_all = self.multi_page = False
            self.paginator = None
            self.show_full_result_count = False
            self.show_admin_actions = False
            return
        super().get_results(request)


def split_api_params(request) -> dict:
    """Move API-owned params out of request.GET. Returns them; keeps the original as ``request.api_query``."""
    original = request.GET
    api = {k: original.get(k) for k in API_PARAMS if k in original}
    cleaned = original.copy()
    for k in API_PARAMS:
        cleaned.pop(k, None)
    cleaned._mutable = False
    request.api_query = original
    request.GET = cleaned
    return api


def build_changelist(model_admin, request, columns, *, per_page=None, schema_only=False) -> ApiChangeList:
    from django.contrib.admin.options import IncorrectLookupParameters
    from django.core.exceptions import SuspiciousOperation

    if per_page is None:
        per_page = resolve(model_admin, "list_per_page", request)
    try:
        return ApiChangeList(
            request,
            model_admin.model,
            [c.name for c in columns],
            (),
            model_admin.get_list_filter(request),
            model_admin.date_hierarchy,
            model_admin.get_search_fields(request),
            model_admin.get_list_select_related(request),
            per_page,
            model_admin.list_max_show_all,
            (),
            model_admin,
            model_admin.get_sortable_by(request),
            model_admin.search_help_text,
            schema_only=schema_only,
        )
    except (IncorrectLookupParameters, SuspiciousOperation) as exc:
        raise BadLookup(str(exc) or "Invalid filter, ordering or page parameter.") from exc


def describe_filters(cl: ApiChangeList) -> list[dict]:
    out = []
    for spec in cl.filter_specs:
        expected = list(spec.expected_parameters())
        choices = []
        for choice in spec.choices(cl):
            qs = QueryDict(choice.get("query_string", "").lstrip("?"))
            choices.append(
                {
                    "display": str(choice.get("display")),
                    "selected": bool(choice.get("selected")),
                    "params": {k: qs.get(k) for k in expected if k in qs},
                }
            )
        out.append({"title": str(spec.title), "parameters": expected, "choices": choices})
    return out


def describe_columns(columns, model_admin, request) -> list[dict]:
    sortable = set(model_admin.get_sortable_by(request) or ())
    out = []
    for index, col in enumerate(columns):
        entry = {"name": col.key, "label": col.label(model_admin), "kind": col.kind, "type": None}
        if col.field is not None:
            entry["type"] = field_type(col.field)
            if getattr(col.field, "choices", None):
                entry["choices"] = [{"value": encode_value(v), "display": str(d)} for v, d in col.field.flatchoices]
        entry["sortable"] = col.name in sortable and (col.kind == "field" or _has_ordering(col, model_admin))
        entry["order_index"] = index  # value to use in ?o=
        out.append(entry)
    return out


def _has_ordering(col, model_admin) -> bool:
    func = col.name if callable(col.name) else getattr(model_admin, col.name, None)
    return bool(getattr(func, "admin_order_field", None))
