"""JSON Schema (draft 2020-12) for write payloads, generated from the admin's own forms.

The schema describes what :func:`admin_api.writes.build_post_data` accepts: the fields of
``get_form()`` (readonly fields are already excluded by the admin) and, under ``"inlines"``,
one array of rows per inline formset prefix.

Two variants:

* ``create``: required = the form's required fields without an initial value (the API fills
  missing fields from the add form's initial values, given as ``default``); inline rows are new.
* ``update``: PATCH semantics, nothing is required; inline rows may carry ``pk`` (update that
  row) and ``"_delete": true``. Rows without ``pk`` are new rows.

Vendor keywords: ``x-related-model`` (``"app_label.model_name"``) on relation fields.
"""

from __future__ import annotations

from django import forms
from django.db import models
from django.utils.html import strip_tags
from django.utils.translation import gettext_lazy as _

from .detail import inline_prefixes
from .encoders import encode_value
from .writes import INLINES_KEY, ROW_DELETE_KEY, form_initial

# ModelAdmin.formfield_for_manytomany appends this widget hint to M2M help texts
_M2M_WIDGET_HINT = _("Hold down “Control”, or “Command” on a Mac, to select more than one.")

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
DECIMAL_PATTERN = r"^-?\d+(\.\d+)?$"


def _nullable(schema: dict) -> dict:
    if "enum" in schema:
        return {**schema, "enum": [*schema["enum"], None]}
    t = schema.get("type")
    if t is None:
        return schema
    types = t if isinstance(t, list) else [t]
    return {**schema, "type": [*types, "null"]} if "null" not in types else schema


def _pk_schema(model, to_field_name=None) -> dict:
    field = model._meta.get_field(to_field_name) if to_field_name else model._meta.pk
    if isinstance(field, (models.AutoField, models.IntegerField)):
        return {"type": "integer"}
    if isinstance(field, models.UUIDField):
        return {"type": "string", "format": "uuid"}
    return {"type": "string"}


def field_schema(form_field) -> dict | None:
    """JSON Schema for one form field, or ``None`` if the JSON API cannot set it (files)."""
    f = form_field
    if isinstance(f, forms.FileField):
        return None
    if isinstance(f, forms.ModelMultipleChoiceField):
        model = f.queryset.model
        schema = {"type": "array", "items": _pk_schema(model, f.to_field_name), "uniqueItems": True}
        schema["x-related-model"] = f"{model._meta.app_label}.{model._meta.model_name}"
    elif isinstance(f, forms.ModelChoiceField):
        model = f.queryset.model
        schema = {**_pk_schema(model, f.to_field_name)}
        schema["x-related-model"] = f"{model._meta.app_label}.{model._meta.model_name}"
    elif isinstance(f, forms.MultipleChoiceField):
        schema = {"type": "array", "items": {"enum": _choice_values(f)}, "uniqueItems": True}
    elif isinstance(f, forms.ChoiceField):
        schema = {"enum": _choice_values(f)}
    elif isinstance(f, forms.NullBooleanField):
        schema = {"type": ["boolean", "null"]}
    elif isinstance(f, forms.BooleanField):
        schema = {"type": "boolean"}
    elif isinstance(f, forms.DecimalField):
        # strings keep precision; plain JSON numbers are accepted too
        schema = {"type": ["string", "number"], "pattern": DECIMAL_PATTERN}
        if f.max_digits is not None and f.decimal_places is not None:
            schema["description"] = f"Decimal: max {f.max_digits} digits, {f.decimal_places} decimal places."
    elif isinstance(f, forms.FloatField):
        schema = {"type": "number"}
    elif isinstance(f, forms.IntegerField):
        schema = {"type": "integer"}
        if f.min_value is not None:
            schema["minimum"] = f.min_value
        if f.max_value is not None:
            schema["maximum"] = f.max_value
    elif isinstance(f, (forms.SplitDateTimeField, forms.DateTimeField)):
        schema = {"type": "string", "format": "date-time"}
    elif isinstance(f, forms.DateField):
        schema = {"type": "string", "format": "date"}
    elif isinstance(f, forms.TimeField):
        schema = {"type": "string", "format": "time"}
    elif isinstance(f, forms.DurationField):
        schema = {"type": "string", "description": "Duration, e.g. '1 02:03:04' or ISO 8601 'P1DT2H'."}
    elif isinstance(f, forms.UUIDField):
        schema = {"type": "string", "format": "uuid"}
    elif isinstance(f, forms.JSONField):
        schema = {}
    elif isinstance(f, forms.EmailField):
        schema = {"type": "string", "format": "email"}
    elif isinstance(f, forms.URLField):
        schema = {"type": "string", "format": "uri"}
    else:
        schema = {"type": "string"}
        if getattr(f, "max_length", None):
            schema["maxLength"] = f.max_length
        if getattr(f, "min_length", None):
            schema["minLength"] = f.min_length

    if f.label:
        schema["title"] = str(f.label)
    help_text = strip_tags(str(f.help_text or "")).replace(str(_M2M_WIDGET_HINT), "").strip()
    if help_text:
        schema["description"] = f"{schema['description']} {help_text}" if "description" in schema else help_text
    if not f.required and not isinstance(f, (forms.BooleanField, forms.JSONField)) and schema.get("type") != "array":
        schema = _nullable(schema)
    return schema


def _choice_values(f) -> list:
    out = []
    for value, label in f.choices:
        if isinstance(label, (list, tuple)):  # optgroup
            out.extend(encode_value(v) for v, _ in label if v != "")
        elif value != "":
            out.append(encode_value(value))
    return out


def _has_value(value) -> bool:
    return value not in (None, "", [], ())


def form_object_schema(form_class, *, required: bool, exclude=()) -> dict:
    """Object schema for a form class. With ``required``, a form-required field is only required
    in the payload when the add form has no initial value for it (the API fills those in)."""
    initial = form_initial(form_class, None) if required else {}
    properties, required_names = {}, []
    for name, f in form_class.base_fields.items():
        if name in exclude:
            continue
        schema = field_schema(f)
        if schema is None:
            continue
        if required and _has_value(initial.get(name)):
            default = encode_value(initial[name])
            if isinstance(default, (str, int, float, bool, list)) or default is None:
                schema["default"] = default
        properties[name] = schema
        if required and f.required and not _has_value(initial.get(name)):
            required_names.append(name)
    out = {"type": "object", "properties": properties, "additionalProperties": False}
    if required_names:
        out["required"] = required_names
    return out


def _inline_schemas(model_admin, request, obj, *, change: bool) -> dict:
    out = {}
    for prefix, FormSet, inline in inline_prefixes(model_admin, request, obj, change):
        fk = getattr(FormSet, "fk", None)
        exclude = {fk.name} if fk is not None else set()
        row = form_object_schema(FormSet.form, required=not change, exclude=exclude)
        opts = FormSet.model._meta
        description = f"Rows of {opts.verbose_name_plural}."
        if change:
            row["properties"] = {
                "pk": {**_pk_schema(FormSet.model), "description": "Existing row to update; omit for a new row."},
                ROW_DELETE_KEY: {"type": "boolean", "description": "Delete the existing row given by pk."},
                **row["properties"],
            }
            initial = form_initial(FormSet.form, None)
            required = [
                n
                for n, f in FormSet.form.base_fields.items()
                if f.required and n not in exclude and not _has_value(initial.get(n))
            ]
            description += " Rows not mentioned are kept unchanged."
            if required:
                description += f" New rows (without pk) need: {', '.join(required)}."
        schema = {"type": "array", "items": row, "description": description}
        if not change and FormSet.max_num is not None:
            schema["maxItems"] = FormSet.max_num
        if not change and FormSet.min_num:
            schema["minItems"] = FormSet.min_num
        schema["x-related-model"] = f"{opts.app_label}.{opts.model_name}"
        out[prefix] = schema
    return out


def payload_schema(model_admin, request, obj=None, *, change: bool) -> dict:
    """Schema of the JSON body for create (``change=False``) or update (``change=True``)."""
    form_class = model_admin.get_form(request, obj, change=change)
    schema = {"$schema": SCHEMA_DIALECT, **form_object_schema(form_class, required=not change)}
    inlines = _inline_schemas(model_admin, request, obj, change=change)
    if inlines:
        schema["properties"][INLINES_KEY] = {
            "type": "object",
            "properties": inlines,
            "additionalProperties": False,
            "description": "Inline rows, keyed by inline prefix.",
        }
    return schema


def input_schemas(model_admin, request, obj=None) -> dict:
    """``{"create": schema | None, "update": schema | None}`` for what ``request.user`` may do."""
    return {
        "create": payload_schema(model_admin, request, None, change=False)
        if obj is None and model_admin.has_add_permission(request)
        else None,
        "update": payload_schema(model_admin, request, obj, change=True)
        if model_admin.has_change_permission(request, obj)
        else None,
    }
