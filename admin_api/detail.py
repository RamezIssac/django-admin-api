"""Serialize one object, and its inlines, the way the admin change form shows it."""

from __future__ import annotations

from django.contrib.admin.utils import flatten_fieldsets, lookup_field
from django.core.exceptions import FieldDoesNotExist

from .decorators import is_api_display
from .encoders import encode_value
from .resolver import resolve


def _model_field(model, name):
    if not isinstance(name, str):
        return None
    try:
        return model._meta.get_field(name)
    except FieldDoesNotExist:
        return None


def field_value(obj, field):
    if field.many_to_many:
        return [encode_value(o) for o in getattr(obj, field.name).all()]
    if field.is_relation and (field.many_to_one or field.one_to_one) and field.concrete:
        return encode_value(getattr(obj, field.name))
    return encode_value(field.value_from_object(obj))


def _name_key(name):
    return name if isinstance(name, str) else getattr(name, "__name__", repr(name))


def detail_fields(model_admin, request, obj) -> tuple[list, bool]:
    """Return (field names, explicit). Explicit = from ``api_detail_fields`` / ``get_api_detail_fields``."""
    explicit = resolve(model_admin, "detail_fields", request, obj)
    if explicit is not None:
        return list(explicit), True
    return flatten_fieldsets(model_admin.get_fieldsets(request, obj)), False


def serialize_values(model_admin, obj, names, explicit) -> dict:
    model = type(obj)
    out = {}
    for name in names:
        field = _model_field(model, name)
        if field is not None:
            out[name] = field_value(obj, field)
            continue
        func = name if callable(name) else getattr(model_admin, name, None) or getattr(model, name, None)
        if explicit or is_api_display(func):
            _f, _attr, value = lookup_field(name, obj, model_admin)
            out[_name_key(name)] = encode_value(value)
    return out


def inline_prefixes(model_admin, request, obj, change):
    """``[(prefix, FormSet, inline)]`` with the same prefix rules as ``ModelAdmin._create_formsets``."""
    out = []
    prefixes = {}
    args = [request, obj] if change else [request]
    for FormSet, inline in model_admin.get_formsets_with_inlines(*args):
        prefix = FormSet.get_default_prefix()
        prefixes[prefix] = prefixes.get(prefix, 0) + 1
        if prefixes[prefix] != 1 or not prefix:
            prefix = f"{prefix}-{prefixes[prefix]}"
        out.append((prefix, FormSet, inline))
    return out


def inline_queryset(FormSet, inline, request, obj):
    qs = inline.get_queryset(request)
    fk = getattr(FormSet, "fk", None)
    if fk is not None:
        return qs.filter(**{fk.name: obj})
    ct_field = getattr(FormSet, "ct_field", None)  # generic inlines
    if ct_field is not None:
        from django.contrib.contenttypes.models import ContentType

        ct = ContentType.objects.get_for_model(obj, for_concrete_model=FormSet.for_concrete_model)
        return qs.filter(**{ct_field.name: ct, FormSet.ct_fk_field.name: obj.pk})
    return qs.none()


def inline_field_names(FormSet, inline, request, obj) -> list:
    names = flatten_fieldsets(inline.get_fieldsets(request, obj))
    excluded = set()
    fk = getattr(FormSet, "fk", None)
    if fk is not None:
        excluded.add(fk.name)
    return [n for n in names if n not in excluded]


def serialize_inlines(model_admin, request, obj) -> dict:
    out = {}
    for prefix, FormSet, inline in inline_prefixes(model_admin, request, obj, change=True):
        names = inline_field_names(FormSet, inline, request, obj)
        qs = inline_queryset(FormSet, inline, request, obj)
        related = [n for n in names if (f := _model_field(qs.model, n)) is not None and f.many_to_one]
        many = [n for n in names if (f := _model_field(qs.model, n)) is not None and f.many_to_many]
        if related:
            qs = qs.select_related(*related)
        if many:
            qs = qs.prefetch_related(*many)
        if not qs.query.order_by and not qs.ordered:
            qs = qs.order_by("pk")
        out[prefix] = [
            {"pk": encode_value(row.pk), **serialize_values(inline, row, names, explicit=False)} for row in qs
        ]
    return out


def serialize_object(model_admin, request, obj) -> dict:
    names, explicit = detail_fields(model_admin, request, obj)
    payload = {"pk": encode_value(obj.pk), "str": str(obj)}
    payload.update(serialize_values(model_admin, obj, names, explicit))
    inlines = serialize_inlines(model_admin, request, obj)
    if inlines:
        payload["inlines"] = inlines
    return payload
