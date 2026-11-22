"""Record history (LogEntry): the get_history MCP tool, REST ``<pk>/history/`` and the LogEntry of write views."""

import datetime
import json
from decimal import Decimal

import pytest
from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.admin.options import get_content_type_for_model
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission

from admin_api.encoders import encode_value
from admin_api.mcp import AdminMcpSite
from admin_api.models import ApiToken
from tests.testapp import admin as testadmin
from tests.testapp.models import Client, Sales
from tests.urls import api_site

T0 = datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.timezone.utc)


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
def sale(db):
    return Sales.objects.create(slug="S-1", client=Client.objects.create(name="Ahmed"), value=Decimal(10))


def log(user, obj, flag, message, minutes=0, object_id=None):
    return LogEntry.objects.create(
        user=user,
        content_type=get_content_type_for_model(obj),
        object_id=object_id or str(obj.pk),
        object_repr=str(obj),
        action_flag=flag,
        change_message=message,
        action_time=T0 + datetime.timedelta(minutes=minutes),
    )


def history(call, obj, limit=None, **kwargs):
    return call("get_history", {"model": f"testapp.{obj._meta.model_name}", "id": obj.pk, "limit": limit}, **kwargs)


# --------------------------------------------------------------------------------------------
# get_history
# --------------------------------------------------------------------------------------------
def test_history_newest_first_in_the_admins_wording(call, superuser, sale):
    added = log(superuser, sale, ADDITION, '[{"added": {}}]')
    changed = log(superuser, sale, CHANGE, '[{"changed": {"fields": ["Value"]}}]', minutes=5)
    log(superuser, sale, DELETION, "", minutes=5)  # same time: the later row first
    out, err = history(call, sale)
    assert not err, out
    when, earlier = encode_value(changed.action_time), encode_value(added.action_time)
    assert out == {
        "object": {"id": sale.pk, "label": str(sale)},
        "history": [
            {"when": when, "user": "root", "action": "deleted", "message": ""},
            {"when": when, "user": "root", "action": "changed", "message": "Changed Value."},
            {"when": earlier, "user": "root", "action": "added", "message": "Added."},
        ],
    }


def test_history_of_mcp_writes(call, sale):
    assert not call("update_object", {"model": "testapp.sales", "id": sale.pk, "data": {"value": "12"}})[1]
    out, _ = call("create_object", {"model": "testapp.client", "data": {"name": "Amal"}})
    [changed] = history(call, sale)[0]["history"]
    assert (changed["action"], changed["message"]) == ("changed", "Changed Value. Via MCP.")
    [added] = history(call, Client.objects.get(pk=out["object"]["id"]))[0]["history"]
    assert (added["action"], added["message"]) == ("added", "Added. Via MCP.")


def test_history_only_this_record(call, superuser, sale):
    other = Sales.objects.create(slug="S-2", client=sale.client)
    log(superuser, other, CHANGE, "other sale")
    log(superuser, sale.client, CHANGE, "a client with the sale's ID", object_id=str(sale.pk))
    out, err = history(call, sale)
    assert not err and out["history"] == []


def test_history_limit_and_hint(call, superuser, sale):
    for minute in range(3):
        log(superuser, sale, CHANGE, f"change {minute}", minutes=minute)
    out, _ = history(call, sale, limit=2)
    assert [e["message"] for e in out["history"]] == ["change 2", "change 1"]
    assert out["hint"] == "Only the newest 2 entries are shown; limit goes up to 100."
    out, _ = history(call, sale, limit=3)
    assert len(out["history"]) == 3 and "hint" not in out
    for limit in (0, 101, "2"):
        out, err = history(call, sale, limit=limit)
        assert err and out["error"] == "invalid_arguments", limit


def test_history_default_limit_is_20(call, superuser, sale):
    for minute in range(21):
        log(superuser, sale, CHANGE, f"change {minute}", minutes=minute)
    out, _ = history(call, sale)
    assert len(out["history"]) == 20 and out["history"][-1]["message"] == "change 1"
    assert len(history(call, sale, limit=100)[0]["history"]) == 21


def test_history_follows_the_admins_queryset_and_permissions(call, sale, monkeypatch):
    # ClientAdmin's API queryset only has names starting with "A"
    out, err = history(call, Client.objects.create(name="Bassem"))
    assert err and out["error"] == "not_found"
    out, err = call("get_history", {"model": "testapp.sales", "id": 999})
    assert err and out["error"] == "not_found"
    out, err = call("get_history", {"model": "testapp.sales"})
    assert err and out["error"] == "invalid_arguments"

    monkeypatch.setattr(
        testadmin.SalesAdmin, "has_view_or_change_permission", lambda self, request, obj=None: obj is None
    )
    out, err = history(call, sale)
    assert err and out["error"] == "permission_denied"


def test_history_needs_view_permission(call, sale):
    viewer = get_user_model().objects.create_user("viewer", "v@x.io", "pw", is_staff=True)
    out, err = history(call, sale, user=viewer)
    assert err and out["error"] == "permission_denied"
    viewer.user_permissions.add(Permission.objects.get(codename="view_sales"))
    viewer = get_user_model().objects.get(pk=viewer.pk)
    assert not history(call, sale, user=viewer)[1]
    out, err = history(call, sale, scopes=["testapp.client:view"])
    assert err and out["error"] == "permission_denied"
    assert not history(call, sale, scopes=["testapp.sales:view"])[1]


def test_history_tool_on_read_only_sites():
    tool = AdminMcpSite(api_site, read_only=True).get_tool("get_history")
    assert tool is not None and tool.annotations["readOnlyHint"] is True
    assert tool.input_schema["required"] == ["model", "id"]


# --------------------------------------------------------------------------------------------
# REST
# --------------------------------------------------------------------------------------------
def test_rest_history(client, superuser, sale):
    client.force_login(superuser)
    log(superuser, sale, CHANGE, "first")
    log(superuser, sale, CHANGE, "second", minutes=1)
    resp = client.get(f"/api/v1/testapp/sales/{sale.pk}/history/?limit=1")
    body = json.loads(resp.content)
    assert resp.status_code == 200, body
    assert body["object"] == {"pk": sale.pk, "str": str(sale)}
    assert [e["message"] for e in body["history"]] == ["second"] and "hint" in body
    assert client.get(f"/api/v1/testapp/sales/{sale.pk}/history/?limit=x").status_code == 400
    assert client.post(f"/api/v1/testapp/sales/{sale.pk}/history/").status_code == 405


# --------------------------------------------------------------------------------------------
# write views log a change
# --------------------------------------------------------------------------------------------
def test_write_view_logs_its_title_not_its_params(call, sale):
    out, err = call("post_sale", {"id": sale.pk, "note": "secret-note"})
    assert not err, out
    entry = LogEntry.objects.get(object_id=str(sale.pk))
    assert (entry.action_flag, entry.get_change_message()) == (CHANGE, "Ran Post. Via MCP.")


def test_write_view_logs_through_rest(client, superuser, sale):
    client.force_login(superuser)
    resp = client.post(f"/api/v1/testapp/sales/{sale.pk}/views/post/", "{}", content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert LogEntry.objects.get(object_id=str(sale.pk)).change_message == "Ran Post."


def test_write_view_without_a_change_logs_nothing(call, sale):
    assert not call("post_sale", {"id": sale.pk, "dry_run": True})[1]  # rolled back
    sale.status = "posted"
    sale.save()
    out, err = call("post_sale", {"id": sale.pk})  # ValidationError: Already posted.
    assert err and out["error"] == "validation_failed"
    assert not call("run_view", {"model": "testapp.sales", "view": "publish_all"})[1]  # no single record
    assert not call("run_view", {"model": "testapp.client", "view": "statement", "id": sale.client.pk})[1]  # reads
    assert not LogEntry.objects.exists()
