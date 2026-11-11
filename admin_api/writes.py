"""The write side: JSON payload → admin form POST data → ``process_changeform`` pipeline.

The API never validates or saves by itself. It translates the JSON body into exactly the POST
data the HTML change form would submit (main form + inline management forms), puts it on a
copy of the request with ``method = "POST"``, and runs the admin's own changeform pipeline:

* ``model_admin.process_changeform(request, obj, change)`` when the admin defines it
  (erp_framework's ``EntityAdmin`` does, to run its hooks), otherwise
* :func:`default_process_changeform`, which mirrors ``ModelAdmin._changeform_view`` minus
  the rendering.

So ``get_form``, ``_create_formsets``, ``save_form``, ``save_model``, ``save_related``,
``save_formset``, ``construct_change_message`` and ``LogEntry`` all behave as in the admin.
"""

from __future__ import annotations

import copy
import datetime
import json
from contextlib import nullcontext
from dataclasses import dataclass, field

from django import forms
from django.contrib.messages.storage.base import BaseStorage
from django.db import IntegrityError, models, router, transaction
from django.forms.formsets import all_valid
from django.http import QueryDict
from django.utils import timezone
from django.utils.datastructures import MultiValueDict
from django.utils.dateparse import parse_datetime

from .detail import inline_prefixes, inline_queryset
from .encoders import encode_value
from .errors import ApiError

IGNORED_KEYS = frozenset({"pk", "str"})
INLINES_KEY = "inlines"
ROW_DELETE_KEY = "_delete"


class InvalidJson(ApiError):
    status = 400
    code = "invalid_json"


class ValidationFailed(ApiError):
    status = 400
    code = "validation_failed"


class Conflict(ApiError):
    status = 409
    code = "conflict"


@dataclass
class ChangeformResult:
    valid: bool
    obj: models.Model | None
    form: forms.BaseForm
    formsets: list = field(default_factory=list)
    change: bool = False


# --------------------------------------------------------------------------------------------
# request helpers
# --------------------------------------------------------------------------------------------
class CaptureMessageStorage(BaseStorage):
    """In-memory message storage: messages raised during an API call are returned, not persisted."""

    def _get(self, *args, **kwargs):
        return [], True

    def _store(self, messages, response, *args, **kwargs):
        return []

    def captured(self) -> list[dict]:
        return [{"level": m.level_tag or str(m.level), "text": str(m.message)} for m in self._queued_messages]


def capture_messages(request) -> CaptureMessageStorage:
    storage = CaptureMessageStorage(request)
    request._messages = storage
    return storage


def parse_json_body(request) -> dict:
    if not request.body:
        return {}
    try:
        payload = json.loads(request.body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidJson("Request body is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise InvalidJson("Request body must be a JSON object.")
    return payload


def as_post_request(request, data: QueryDict, files=None):
    """A shallow copy of ``request`` that looks like the admin change form was submitted."""
    post_request = copy.copy(request)
    post_request.method = "POST"
    post_request._post = data
    post_request._files = files if files is not None else MultiValueDict()
    capture_messages(post_request)
    return post_request


# --------------------------------------------------------------------------------------------
# JSON value → POST strings
# --------------------------------------------------------------------------------------------
def _pk_of(value):
    if isinstance(value, models.Model):
        return value.pk
    if isinstance(value, dict) and "pk" in value:
        return value["pk"]
    return value


def _scalar(value) -> str:
    value = _pk_of(value)
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return value
    encoded = encode_value(value)
    return encoded if isinstance(encoded, str) else str(encoded)


def _split_datetime(value):
    if value in (None, ""):
        return ["", ""]
    if isinstance(value, (list, tuple)):
        return [_scalar(v) for v in value]
    if isinstance(value, str):
        parsed = parse_datetime(value)
        if parsed is None:
            return [value, ""]
        value = parsed
    if isinstance(value, datetime.datetime):
        if timezone.is_aware(value):
            value = timezone.make_naive(value)
        return [value.date().isoformat(), value.time().isoformat()]
    return [_scalar(value), ""]


def field_post_values(name: str, form_field, value) -> dict[str, list[str]]:
    """POST key(s) and value list(s) for one form field."""
    if isinstance(form_field, forms.SplitDateTimeField):
        parts = _split_datetime(value)
        return {f"{name}_{i}": [p] for i, p in enumerate(parts)}
    if isinstance(form_field, forms.MultiValueField):
        parts = value if isinstance(value, (list, tuple)) else [value]
        return {f"{name}_{i}": [_scalar(p)] for i, p in enumerate(parts)}
    if isinstance(form_field, forms.JSONField):
        return {name: ["" if value is None else json.dumps(value)]}
    if isinstance(form_field, (forms.MultipleChoiceField, forms.ModelMultipleChoiceField)):
        return {name: [_scalar(v) for v in (value or [])]}
    if isinstance(form_field, forms.BooleanField) and not isinstance(form_field, forms.NullBooleanField):
        if isinstance(value, str):
            value = value.strip().lower() not in ("", "0", "false", "off", "no")
        return {name: ["on"] if value else []}
    if isinstance(value, (list, tuple)):
        return {name: [_scalar(v) for v in value]}
    return {name: [_scalar(value)]}


def form_initial(form_class, instance, **form_kwargs) -> dict:
    """Current values as the change form would render them (python values)."""
    form = form_class(instance=instance, **form_kwargs)
    return {name: form.get_initial_for_field(f, name) for name, f in form.fields.items()}


def _put(data: QueryDict, key: str, values: list[str]):
    data.setlist(key, values)


def _fill_form(data, prefix, base_fields, values, errors, error_key=None):
    for key, value in values.items():
        if key not in base_fields:
            continue
        if isinstance(base_fields[key], forms.FileField):
            errors.setdefault(error_key or key, []).append("File upload is not supported by the JSON API yet.")
            continue
        full = f"{prefix}-{key}" if prefix else key
        for post_key, post_values in field_post_values(full, base_fields[key], value).items():
            _put(data, post_key, post_values)


def _check_keys(payload: dict, allowed: set, errors: dict, where: str = ""):
    for key in payload:
        if key not in allowed:
            errors.setdefault(f"{where}{key}", []).append("Unknown or read-only field.")


# --------------------------------------------------------------------------------------------
# payload → POST data
# --------------------------------------------------------------------------------------------
def build_post_data(model_admin, request, obj, payload: dict, *, change: bool, partial: bool) -> QueryDict:
    """Translate the JSON payload into the change form's POST data.

    ``partial`` (PATCH): missing fields keep their current values. Otherwise (POST/PUT)
    missing fields take the add-form initial values (model defaults).

    Inlines: ``{"inlines": {"<prefix>": [row, ...]}}``. A row with ``pk`` updates that
    existing row (``"_delete": true`` deletes it); a row without ``pk`` is created. Existing
    rows that are not mentioned are kept unchanged.
    """
    data = QueryDict(mutable=True)
    errors: dict = {}
    inline_errors: dict = {}

    form_class = model_admin.get_form(request, obj, change=change)
    base_fields = form_class.base_fields
    _check_keys(payload, set(base_fields) | IGNORED_KEYS | {INLINES_KEY}, errors)

    initial = form_initial(form_class, obj if (change and partial) else None)
    _fill_form(data, "", base_fields, {**initial, **{k: v for k, v in payload.items() if k in base_fields}}, errors)

    inlines_payload = payload.get(INLINES_KEY) or {}
    if not isinstance(inlines_payload, dict):
        errors.setdefault(INLINES_KEY, []).append("Must be an object keyed by inline prefix.")
        inlines_payload = {}

    known_prefixes = set()
    for prefix, FormSet, inline in inline_prefixes(model_admin, request, obj, change):
        known_prefixes.add(prefix)
        rows = inlines_payload.get(prefix) or []
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            inline_errors[prefix] = {"non_form_errors": ["Must be a list of objects."]}
            continue
        _build_inline(data, prefix, FormSet, inline, request, obj, change, rows, inline_errors)

    for prefix in inlines_payload:
        if prefix not in known_prefixes:
            inline_errors[prefix] = {"non_form_errors": ["Unknown inline (or no permission)."]}

    if errors or inline_errors:
        raise ValidationFailed(**_envelope(errors, [], inline_errors))
    data._mutable = False
    return data


def _build_inline(data, prefix, FormSet, inline, request, obj, change, rows, inline_errors):
    form_class = FormSet.form
    base_fields = form_class.base_fields
    pk_name = FormSet.model._meta.pk.name
    fk = getattr(FormSet, "fk", None)
    allowed = set(base_fields) | IGNORED_KEYS | {ROW_DELETE_KEY}
    if fk is not None:
        allowed.discard(fk.name)

    row_errors: list = []
    existing = list(inline_queryset(FormSet, inline, request, obj).order_by("pk")) if change else []
    by_pk = {str(o.pk): o for o in existing}
    updates, new_rows = {}, []
    # The admin's inline forms silently ignore rows the user may not add/change (and the formset
    # rejects deletes only when can_delete is off). The API reports them instead.
    can_add = inline.has_add_permission(request, obj)
    can_change = inline.has_change_permission(request, obj)
    can_delete = FormSet.can_delete and inline.has_delete_permission(request, obj)
    for index, row in enumerate(rows):
        errs: dict = {}
        _check_keys(row, allowed, errs)
        if "pk" in row and row["pk"] is not None:
            if str(row["pk"]) not in by_pk:
                errs.setdefault("pk", []).append("No such row in this inline.")
            else:
                updates[str(row["pk"])] = row
                if row.get(ROW_DELETE_KEY) and not can_delete:
                    errs.setdefault(ROW_DELETE_KEY, []).append("You may not delete rows of this inline.")
                if not can_change and any(k in base_fields for k in row):
                    errs.setdefault("non_field_errors", []).append("You may not change rows of this inline.")
        else:
            if row.get(ROW_DELETE_KEY):
                errs.setdefault(ROW_DELETE_KEY, []).append("Only existing rows (with pk) can be deleted.")
            elif not can_add:
                errs.setdefault("non_field_errors", []).append("You may not add rows to this inline.")
            new_rows.append(row)
        if errs:
            row_errors.append({"index": index, "pk": row.get("pk"), "errors": errs})

    i = 0
    for instance in existing:
        row = updates.get(str(instance.pk), {})
        values = {**form_initial(form_class, instance), **{k: v for k, v in row.items() if k in base_fields}}
        errs: dict = {}
        _fill_form(data, f"{prefix}-{i}", base_fields, values, errs)
        _put(data, f"{prefix}-{i}-{pk_name}", [str(instance.pk)])
        if fk is not None:
            _put(data, f"{prefix}-{i}-{fk.name}", [str(obj.pk)])
        if row.get(ROW_DELETE_KEY):
            _put(data, f"{prefix}-{i}-DELETE", ["on"])
        if errs:
            row_errors.append({"index": i, "pk": instance.pk, "errors": errs})
        i += 1
    for row in new_rows:
        values = {**form_initial(form_class, None), **{k: v for k, v in row.items() if k in base_fields}}
        errs = {}
        _fill_form(data, f"{prefix}-{i}", base_fields, values, errs)
        if errs:
            row_errors.append({"index": i, "pk": None, "errors": errs})
        i += 1

    _put(data, f"{prefix}-TOTAL_FORMS", [str(i)])
    _put(data, f"{prefix}-INITIAL_FORMS", [str(len(existing))])
    _put(data, f"{prefix}-MIN_NUM_FORMS", ["0"])
    _put(data, f"{prefix}-MAX_NUM_FORMS", ["1000"])
    if row_errors:
        inline_errors[prefix] = {"rows": row_errors}


# --------------------------------------------------------------------------------------------
# pipeline
# --------------------------------------------------------------------------------------------
def default_process_changeform(model_admin, request, obj, change) -> ChangeformResult:
    """``ModelAdmin._changeform_view`` without rendering. ``request`` is a POST request."""
    add = not change
    ModelForm = model_admin.get_form(request, obj, change=change)
    form = ModelForm(request.POST, request.FILES, instance=obj)
    formsets, _inline_instances = model_admin._create_formsets(request, form.instance, change=change)
    form_validated = form.is_valid()
    new_object = model_admin.save_form(request, form, change=change) if form_validated else form.instance
    if all_valid(formsets) and form_validated:
        model_admin.save_model(request, new_object, form, change)
        model_admin.save_related(request, form, formsets, change)
        change_message = model_admin.construct_change_message(request, form, formsets, add)
        if add:
            model_admin.log_addition(request, new_object, change_message)
        else:
            model_admin.log_change(request, new_object, change_message)
        return ChangeformResult(True, new_object, form, formsets, change)
    return ChangeformResult(False, None, form, formsets, change)


def _revision_context(model_admin, request):
    create_revision = getattr(model_admin, "create_revision", None)  # reversion.admin.VersionAdmin
    return create_revision(request) if callable(create_revision) else nullcontext()


def run_changeform(model_admin, post_request, obj, change):
    pipeline = getattr(model_admin, "process_changeform", None)
    try:
        with transaction.atomic(using=router.db_for_write(model_admin.model)):
            with _revision_context(model_admin, post_request):
                if pipeline is not None:
                    result = pipeline(post_request, obj, change)
                else:
                    result = default_process_changeform(model_admin, post_request, obj, change)
            if not result.valid:
                transaction.set_rollback(True)
    except IntegrityError as exc:
        raise Conflict("The change conflicts with a database constraint.") from exc
    if not result.valid:
        raise ValidationFailed(**result_errors(result))
    return result


# --------------------------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------------------------
def _envelope(fields: dict, non_field: list, inlines: dict) -> dict:
    out = {}
    if fields:
        out["fields"] = fields
    if non_field:
        out["non_field_errors"] = non_field
    if inlines:
        out["inlines"] = inlines
    return out


def result_errors(result) -> dict:
    fields = {k: [str(m) for m in v] for k, v in result.form.errors.items() if k != "__all__"}
    non_field = [str(m) for m in result.form.non_field_errors()]
    inlines = {}
    for formset in result.formsets:
        rows = []
        for index, form in enumerate(formset.forms):
            if form.errors:
                pk = form.instance.pk if index < formset.initial_form_count() else None
                rows.append({"index": index, "pk": encode_value(pk), "errors": _form_errors(form)})
        non_form = [str(m) for m in formset.non_form_errors()]
        if rows or non_form:
            entry = {}
            if rows:
                entry["rows"] = rows
            if non_form:
                entry["non_form_errors"] = non_form
            inlines[formset.prefix] = entry
    if not (fields or non_field or inlines):
        # e.g. erp_framework's whole_changeform_validation returned False without adding an error
        non_field = ["The change was rejected by validation."]
    return _envelope(fields, non_field, inlines)


def _form_errors(form) -> dict:
    return {("non_field_errors" if k == "__all__" else k): [str(m) for m in v] for k, v in form.errors.items()}


# --------------------------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------------------------
def save_from_payload(model_admin, request, obj, payload: dict, *, change: bool, partial: bool):
    data = build_post_data(model_admin, request, obj, payload, change=change, partial=partial)
    post_request = as_post_request(request, data)
    result = run_changeform(model_admin, post_request, obj, change)
    return result, post_request._messages.captured()


# --------------------------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------------------------
_FORM_TYPES = [
    (forms.BooleanField, "boolean"),
    (forms.IntegerField, "integer"),
    (forms.DecimalField, "decimal"),
    (forms.FloatField, "number"),
    (forms.SplitDateTimeField, "datetime"),
    (forms.DateTimeField, "datetime"),
    (forms.DateField, "date"),
    (forms.TimeField, "time"),
    (forms.DurationField, "duration"),
    (forms.UUIDField, "uuid"),
    (forms.JSONField, "json"),
    (forms.FileField, "file"),
    (forms.ModelMultipleChoiceField, "related_many"),
    (forms.ModelChoiceField, "related"),
    (forms.MultipleChoiceField, "choice_many"),
    (forms.ChoiceField, "choice"),
]


def describe_form_field(name, f) -> dict:
    entry = {"name": name, "label": str(f.label or name), "required": f.required, "type": "string"}
    for cls, type_name in _FORM_TYPES:
        if isinstance(f, cls):
            entry["type"] = type_name
            break
    if f.help_text:
        entry["help_text"] = str(f.help_text)
    if isinstance(f, forms.ModelChoiceField):
        opts = f.queryset.model._meta
        entry["related_model"] = f"{opts.app_label}.{opts.model_name}"
    elif isinstance(f, forms.ChoiceField):
        entry["choices"] = [{"value": encode_value(v), "display": str(d)} for v, d in f.choices if v != ""]
    return entry


def describe_form(model_admin, request) -> dict:
    form_class = model_admin.get_form(request, None, change=False)
    inlines = []
    for prefix, FormSet, inline in inline_prefixes(model_admin, request, None, change=False):
        fk = getattr(FormSet, "fk", None)
        fields = [describe_form_field(n, f) for n, f in FormSet.form.base_fields.items() if fk is None or n != fk.name]
        opts = FormSet.model._meta
        inlines.append(
            {
                "prefix": prefix,
                "model": f"{opts.app_label}.{opts.model_name}",
                "fields": fields,
                "can_delete": bool(FormSet.can_delete),
                "min_num": FormSet.min_num,
                "max_num": FormSet.max_num,
            }
        )
    return {
        "fields": [describe_form_field(n, f) for n, f in form_class.base_fields.items()],
        "readonly": [str(n) for n in model_admin.get_readonly_fields(request, None)],
        "inlines": inlines,
        "notes": "Describes the add form. On change, readonly fields may differ per object.",
    }
