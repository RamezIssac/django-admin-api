import json
from decimal import Decimal

import pytest
from django import forms
from jsonschema import Draft202012Validator

from admin_api.jsonschema import field_schema
from admin_api.writes import describe_form_field
from tests.testapp.models import Client, Sales, SalesItem, Tag

SALES = "/api/v1/testapp/sales/"


@pytest.fixture
def api(client, superuser):
    client.force_login(superuser)
    return client


def schemas(api, model="sales"):
    return json.loads(api.get(f"/api/v1/testapp/{model}/schema/").content)["input_schema"]


def test_schemas_are_valid_json_schema(api):
    for variant in schemas(api).values():
        Draft202012Validator.check_schema(variant)


def test_create_schema(api):
    create = schemas(api)["create"]
    props = create["properties"]
    assert create["required"] == ["slug", "client"]  # fields with defaults are filled in by the API
    assert create["additionalProperties"] is False
    assert props["slug"] == {"type": "string", "maxLength": 20, "title": "Slug"}
    assert props["client"]["type"] == "integer" and props["client"]["x-related-model"] == "testapp.client"
    assert props["status"]["enum"] == ["draft", "posted"] and props["status"]["default"] == "draft"
    assert props["value"]["type"] == ["string", "number"] and "pattern" in props["value"]
    assert props["date"] == {"type": "string", "format": "date", "title": "Date", "default": "2025-01-01"}
    assert props["tags"]["items"] == {"type": "integer"} and "description" not in props["tags"]
    assert "badge" not in props and "doubled" not in props  # readonly
    items = props["inlines"]["properties"]["items"]
    assert items["items"]["required"] == ["product"]
    assert "sales" not in items["items"]["properties"] and "pk" not in items["items"]["properties"]


def test_update_schema(api):
    update = schemas(api)["update"]
    assert "required" not in update
    row = update["properties"]["inlines"]["properties"]["items"]["items"]
    assert "required" not in row
    assert row["properties"]["pk"]["type"] == "integer" and row["properties"]["_delete"]["type"] == "boolean"
    assert "need: product" in update["properties"]["inlines"]["properties"]["items"]["description"]


def test_api_readonly_twin_respected(api):
    create = schemas(api, "client")["create"]
    assert "notes" not in create["properties"]  # ClientAdmin.api_readonly_fields


def test_conforming_payload_is_accepted(api):
    ahmed = Client.objects.create(name="Ahmed")
    tag = Tag.objects.create(name="t")
    create = schemas(api)["create"]
    payload = {
        "slug": "S-9",
        "client": ahmed.pk,
        "tags": [tag.pk],
        "value": "15.50",
        "inlines": {"items": [{"product": "Milk", "quantity": 1, "price": "15.50"}]},
    }
    Draft202012Validator(create).validate(payload)
    resp = api.post(SALES, data=json.dumps(payload), content_type="application/json")
    assert resp.status_code == 201, resp.content
    sale = Sales.objects.get(slug="S-9")
    assert sale.items.count() == 1 and sale.value == Decimal("15.50")

    update = schemas(api)["update"]
    item = SalesItem.objects.get()
    patch = {"status": "posted", "inlines": {"items": [{"pk": item.pk, "quantity": 2}, {"product": "Cheese"}]}}
    Draft202012Validator(update).validate(patch)
    resp = api.patch(f"{SALES}{sale.pk}/", data=json.dumps(patch), content_type="application/json")
    assert resp.status_code == 200, resp.content


def test_nonconforming_payload_is_rejected_by_both(api):
    ahmed = Client.objects.create(name="Ahmed")
    payload = {"slug": "S-9", "client": ahmed.pk, "status": "bogus"}
    errors = list(Draft202012Validator(schemas(api)["create"]).iter_errors(payload))
    assert errors
    assert api.post(SALES, data=json.dumps(payload), content_type="application/json").status_code == 400


def test_no_schemas_without_permission(client, staff_view_clients):
    client.force_login(staff_view_clients)
    assert schemas(client, "client") == {"create": None, "update": None}


@pytest.mark.parametrize(
    "field, expected",
    [
        (forms.DecimalField(max_digits=5, decimal_places=2), {"type": ["string", "number"]}),
        (forms.FloatField(), {"type": "number"}),
        (forms.IntegerField(min_value=1, max_value=9), {"type": "integer", "minimum": 1, "maximum": 9}),
        (forms.BooleanField(required=False), {"type": "boolean"}),
        (forms.NullBooleanField(), {"type": ["boolean", "null"]}),
        (forms.CharField(max_length=5, required=False), {"type": ["string", "null"], "maxLength": 5}),
        (forms.DateTimeField(), {"type": "string", "format": "date-time"}),
        (forms.SplitDateTimeField(), {"type": "string", "format": "date-time"}),
        (forms.TimeField(), {"type": "string", "format": "time"}),
        (forms.UUIDField(), {"type": "string", "format": "uuid"}),
        (forms.EmailField(), {"type": "string", "format": "email"}),
        (forms.ChoiceField(choices=[("", "---"), ("a", "A")], required=False), {"enum": ["a", None]}),
        (forms.MultipleChoiceField(choices=[("a", "A")]), {"type": "array", "items": {"enum": ["a"]}}),
        (forms.JSONField(), {}),
    ],
)
def test_field_schema(field, expected):
    schema = field_schema(field)
    assert {k: schema[k] for k in expected} == expected


def test_file_fields_are_left_out():
    assert field_schema(forms.FileField()) is None


def test_describe_form_types_decimal_and_float():
    assert describe_form_field("v", forms.DecimalField())["type"] == "decimal"
    assert describe_form_field("f", forms.FloatField())["type"] == "number"
