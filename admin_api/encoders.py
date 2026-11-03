"""Python value → JSON-ready value."""

from __future__ import annotations

from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models.fields.files import FieldFile

_encoder = DjangoJSONEncoder()


def encode_value(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, models.Model):
        return {"pk": encode_value(value.pk), "str": str(value)}
    if isinstance(value, FieldFile):
        return value.name or None
    if isinstance(value, (list, tuple, set, frozenset)):
        return [encode_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): encode_value(v) for k, v in value.items()}
    try:
        # datetime/date/time (ISO 8601), timedelta, Decimal, UUID, lazy translation strings
        return _encoder.default(value)
    except TypeError:
        return str(value)


_FIELD_TYPES = [
    (models.BooleanField, "boolean"),
    (models.AutoField, "integer"),
    (models.IntegerField, "integer"),
    (models.DecimalField, "decimal"),
    (models.FloatField, "number"),
    (models.DateTimeField, "datetime"),  # before DateField (subclass)
    (models.DateField, "date"),
    (models.TimeField, "time"),
    (models.DurationField, "duration"),
    (models.UUIDField, "uuid"),
    (models.FileField, "file"),
    (models.JSONField, "json"),
    (models.ForeignKey, "related"),  # covers OneToOneField
    (models.ManyToManyField, "related_many"),
]


def field_type(field) -> str:
    for cls, name in _FIELD_TYPES:
        if isinstance(field, cls):
            return name
    return "string"
