"""MCP answers in business wording: verbose names and "ID" in messages, `id`/`label` keys, hooks."""

import json
import re
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.utils import translation

from admin_api import scopes
from admin_api.errors import PermissionDeniedError
from admin_api.mcp import AdminMcpSite
from admin_api.mcp.sites import DEFAULT_INSTRUCTIONS
from admin_api.mcp.views import make_view
from admin_api.models import ApiToken
from tests.testapp.models import Client, Sales, SalesItem
from tests.urls import api_site

MODEL_PATH = re.compile(r"\b(testapp|auth|admin_api)\.[a-z_]+")


@pytest.fixture
def mcp():
    return AdminMcpSite(api_site)


@pytest.fixture
def call(rf, superuser, mcp):
    def do(name, args=None, *, user=superuser, site=mcp, scopes=None):
        request = rf.post("/mcp/")
        request.user = user
        if scopes is not None:
            request.api_token = ApiToken.objects.create_token(user, "t", scopes=scopes)[0]
        result = site.call_tool(request, name, args or {})
        return result.data, result.is_error

    return do


@pytest.fixture
def data(db):
    ahmed = Client.objects.create(name="Ahmed")
    sale = Sales.objects.create(slug="S-1", client=ahmed, value=Decimal(100))
    item = SalesItem.objects.create(sales=sale, product="Cheese", quantity=1, price=Decimal(100))
    return {"client": ahmed, "sale": sale, "item": item}


@pytest.fixture
def clerk(db):
    user = get_user_model().objects.create_user("clerk", "c@x.io", "pw", is_staff=True)
    user.user_permissions.add(Permission.objects.get(codename="view_sales"))
    return user


# --------------------------------------------------------------------------------------------
# instructions
# --------------------------------------------------------------------------------------------
def test_default_instructions_ask_for_business_language():
    assert DEFAULT_INSTRUCTIONS.startswith("This server gives you the user's business records and documents.")
    assert "Talk to the user in business language" in DEFAULT_INSTRUCTIONS
    assert "not its 'model' path" in DEFAULT_INSTRUCTIONS
    assert 'Say "ID" only when the user asks or two records share a label.' in DEFAULT_INSTRUCTIONS
    assert "listing each candidate's label and the kind of record." in DEFAULT_INSTRUCTIONS
    assert "'ambiguous': ask which one before running anything (not a run per candidate)" in DEFAULT_INSTRUCTIONS
    assert "label and model" not in DEFAULT_INSTRUCTIONS


def test_default_instructions_stay_short():
    assert len(DEFAULT_INSTRUCTIONS) <= 2045


def test_get_instructions_hook_answers_initialize(rf, superuser):
    class PerUserSite(AdminMcpSite):
        def get_instructions(self, request):
            return f"Instructions for {request.user.username}."

    raw = ApiToken.objects.create_token(superuser, "mcp", kind="mcp")[1]
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    }
    request = rf.post(
        "/mcp/",
        data=json.dumps(body),
        content_type="application/json",
        HTTP_ACCEPT="application/json, text/event-stream",
        HTTP_AUTHORIZATION=f"Bearer {raw}",
    )
    response = make_view(PerUserSite(api_site, name="per_user"))(request)
    assert response.status_code == 200, response.content
    assert json.loads(response.content)["result"]["instructions"] == "Instructions for root."


# --------------------------------------------------------------------------------------------
# messages: verbose names and "ID", never app_label.model_name
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name, args, scopes, as_clerk, code",
    [
        ("get_object", {"model": "testapp.sales", "id": 9999}, None, False, "not_found"),
        ("get_object", {"model": "auth.permission", "id": 1}, None, False, "not_found"),
        ("list_objects", {"model": "testapp.tag"}, None, False, "not_found"),  # hidden from MCP
        ("run_view", {"model": "testapp.sales", "view": "nope"}, None, False, "not_found"),
        ("list_objects", {"model": "testapp.client"}, ["testapp.sales:view"], False, "permission_denied"),
        ("update_object", {"model": "testapp.sales", "id": 1, "data": {}}, None, True, "permission_denied"),
        ("delete_object", {"model": "testapp.sales", "id": 1}, None, True, "permission_denied"),
        ("create_object", {"model": "testapp.sales", "data": {}}, None, True, "permission_denied"),
        ("list_objects", {"model": "testapp.client"}, None, True, "permission_denied"),
        ("run_view", {"model": "testapp.sales", "view": "post", "id": 1}, None, True, "permission_denied"),
    ],
)
def test_error_detail_has_no_model_path(call, data, clerk, name, args, scopes, as_clerk, code):
    kwargs = {"scopes": scopes} if scopes else {}
    if as_clerk:
        kwargs["user"] = clerk
    out, err = call(name, args, **kwargs)
    assert err and out["error"] == code, out
    assert out["detail"], out
    assert not MODEL_PATH.search(out["detail"]), out["detail"]


def test_error_wording(call, data, clerk):
    out, _ = call("get_object", {"model": "testapp.sales", "id": 9999})
    assert out["detail"] == "No sales with ID 9999."
    out, _ = call("get_object", {"model": "auth.permission", "id": 1})
    hidden, _ = call("get_object", {"model": "testapp.tag", "id": 1})
    assert out["detail"] == hidden["detail"] == "This kind of record is not available here."
    out, _ = call("update_object", {"model": "testapp.sales", "id": 1, "data": {}}, user=clerk)
    assert out["detail"] == "You may not change this sales."
    out, _ = call("list_objects", {"model": "testapp.sales"}, scopes=["testapp.client:view"])
    assert out["detail"] == "You may not view sales."


def test_scope_message(rf, superuser):
    request = rf.get("/")
    request.api_token = ApiToken.objects.create_token(superuser, "t", scopes=["testapp.client:view"])[0]
    with pytest.raises(PermissionDeniedError, match=r"^This connection may not change sales\.$"):
        scopes.check(request, Sales._meta, "change")


def test_dry_run_note_says_id(call, data):
    args = {"model": "testapp.sales", "id": data["sale"].pk, "data": {"value": "5"}, "dry_run": True}
    out, err = call("update_object", args)
    assert not err and out["note"] == "Nothing was saved; the ID shown is provisional."


def test_view_tool_text(mcp):
    tool = mcp.get_tool("post_sale")
    assert "(Same as run_view 'post' on sales.)" in tool.description
    assert not MODEL_PATH.search(tool.description)
    assert tool.input_schema["properties"]["id"]["description"] == "ID of the sales."


# --------------------------------------------------------------------------------------------
# keys: id and label in MCP, pk and str in REST
# --------------------------------------------------------------------------------------------
def test_get_object_keys(call, data):
    out, err = call("get_object", {"model": "testapp.sales", "id": data["sale"].pk})
    assert not err
    assert list(out)[:2] == ["id", "label"] and out["label"] == "S-1"
    assert out["client"] == {"id": data["client"].pk, "label": "Ahmed"}
    assert out["inlines"]["items"][0]["id"] == data["item"].pk and "pk" not in out["inlines"]["items"][0]


def test_list_objects_keys(call, data):
    out, err = call("list_objects", {"model": "testapp.sales"})
    assert not err and out["columns"][0] == "id"
    row = dict(zip(out["columns"], out["rows"][0]))
    assert row["id"] == data["sale"].pk and row["client"] == {"id": data["client"].pk, "label": "Ahmed"}


def test_list_models_names_first(call, data):
    out, _ = call("list_models")
    assert list(out["models"][0])[:3] == ["name", "name_plural", "model"]


def test_delete_preview_keys(call, data):
    out, err = call("delete_object", {"model": "testapp.client", "id": data["client"].pk})
    assert not err and out["preview"]["object"] == {"id": data["client"].pk, "label": "Ahmed"}


def test_pk_still_accepted_as_alias(call, data):
    by_id, _ = call("get_object", {"model": "testapp.sales", "id": data["sale"].pk})
    by_pk, _ = call("get_object", {"model": "testapp.sales", "pk": data["sale"].pk})
    assert by_id == by_pk
    out, err = call("run_action", {"model": "testapp.sales", "action": "mark_posted", "pks": [data["sale"].pk]})
    assert not err, out
    out, err = call("get_object", {"model": "testapp.sales"})
    assert err and out["error"] == "invalid_arguments"


def test_inline_row_id_updates_the_row(call, data):
    item = data["item"]
    args = {
        "model": "testapp.sales",
        "id": data["sale"].pk,
        "data": {"inlines": {"items": [{"id": item.pk, "quantity": 3}]}},
    }
    out, err = call("update_object", args)
    assert not err, out
    assert SalesItem.objects.filter(sales=data["sale"]).count() == 1
    item.refresh_from_db()
    assert item.quantity == 3


def test_rest_keeps_pk_and_str(client, superuser, data):
    client.force_login(superuser)
    body = client.get(f"/api/v1/testapp/sales/{data['sale'].pk}/").json()
    assert body["pk"] == data["sale"].pk and body["str"] == "S-1"
    assert body["client"] == {"pk": data["client"].pk, "str": "Ahmed"}


# --------------------------------------------------------------------------------------------
# language
# --------------------------------------------------------------------------------------------
def test_get_language_hook_translates_tool_calls(call, data):
    class GermanSite(AdminMcpSite):
        def get_language(self, request):
            return "de"

    args = {"model": "testapp.sales", "id": data["sale"].pk, "data": {"value": "x"}}
    out, err = call("update_object", args, site=GermanSite(api_site))
    assert err and out["fields"]["value"] == ["Bitte eine Zahl eingeben."]
    assert translation.get_language() != "de"


def test_no_language_keeps_the_active_one(call, data):
    args = {"model": "testapp.sales", "id": data["sale"].pk, "data": {"value": "x"}}
    with translation.override("de"):
        out, err = call("update_object", args)
    assert err and out["fields"]["value"] == ["Bitte eine Zahl eingeben."]


# --------------------------------------------------------------------------------------------
# search versus filter
# --------------------------------------------------------------------------------------------
def test_instruction_six_says_filter_not_search():
    assert (
        "Resolve names the user typed with resolve_entities before you filter, report or write." in DEFAULT_INSTRUCTIONS
    )
    assert "pass those IDs as describe_model's filter for that kind of record" in DEFAULT_INSTRUCTIONS
    assert "not as 'search' text, because search does not look into related records" in DEFAULT_INSTRUCTIONS
    assert "an empty search returns 'searched_fields' and 'id_filters'" in DEFAULT_INSTRUCTIONS


def test_empty_search_names_searched_fields_and_id_filters(call, data):
    out, err = call("list_objects", {"model": "testapp.sales", "search": "Ahmed"})
    assert not err and out["count"] == 0
    assert out["searched_fields"] == ["slug"]
    assert out["id_filters"] == ["client__id__exact"]
    assert "resolve_entities" in out["hint"]
    out, err = call("list_objects", {"model": "testapp.sales", "filters": {"client__id__exact": data["client"].pk}})
    assert not err and out["count"] == 1


def test_search_with_rows_has_no_hint(call, data):
    out, err = call("list_objects", {"model": "testapp.sales", "search": "S-1"})
    assert not err and out["count"] == 1 and "searched_fields" not in out
