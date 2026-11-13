"""Custom admin views (``@admin_api.view``) in REST and MCP. See tests/testapp/admin.py:
ClientAdmin.statement / names / rest_only / hidden, SalesAdmin.post / publish_all / as_page /
forbidden."""

import json
from decimal import Decimal

import pytest
from django import forms
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.test import Client as HttpClient

import admin_api
from admin_api.custom_views import declared_views
from admin_api.mcp import AdminMcpSite
from admin_api.models import ApiToken
from admin_api.views.nested import parse_nested
from tests.testapp import admin as testadmin
from tests.testapp.models import Client, Sales
from tests.urls import api_site

CLIENTS = "/api/v1/testapp/client/"
SALES = "/api/v1/testapp/sales/"


@pytest.fixture
def data(db):
    ahmed = Client.objects.create(name="Ahmed")
    Client.objects.create(name="Amal")
    Client.objects.create(name="Bassem")
    sales = [
        Sales.objects.create(slug="S-1", client=ahmed, value=Decimal(10), date="2025-01-01"),
        Sales.objects.create(slug="S-2", client=ahmed, value=Decimal(20), date="2025-02-01", status="posted"),
        Sales.objects.create(slug="S-3", client=ahmed, value=Decimal(30), date="2025-03-01"),
    ]
    testadmin.SAVE_LOG.clear()
    return {"ahmed": ahmed, "sales": sales}


@pytest.fixture
def api(client, superuser):
    client.force_login(superuser)

    def call(method, url, payload=None):
        kwargs = {}
        if payload is not None:
            kwargs = {"data": json.dumps(payload), "content_type": "application/json"}
        resp = getattr(client, method)(url, **kwargs)
        return resp.status_code, json.loads(resp.content)

    return call


def token_client(user, *scopes):
    _token, raw = ApiToken.objects.create_token(user, "t", scopes=list(scopes))
    return HttpClient(HTTP_AUTHORIZATION=f"Bearer {raw}")


def staff(*codenames):
    user = get_user_model().objects.create_user("staff", "s@x.io", "pw", is_staff=True)
    user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
    return user


# --------------------------------------------------------------------------------------------
# declaration
# --------------------------------------------------------------------------------------------
def test_declared_views():
    client_views = declared_views(testadmin.ClientAdmin(Client, testadmin.site))
    assert list(client_views) == ["statement", "names", "rest_only", "hidden"]
    statement = client_views["statement"]
    assert statement.detail and not statement.writes and statement.mcp_tool == "client_statement"
    assert statement.description == "Sales of one client between two dates, with the total."  # docstring
    assert statement.title == "Statement"
    sales_views = declared_views(testadmin.SalesAdmin(Sales, testadmin.site))
    assert sales_views["post"].writes  # permissions beyond view
    assert sales_views["publish_all"].writes
    assert not sales_views["forbidden"].writes  # bare decorator: a read view
    assert sales_views["as_page"].permissions == ("change",)  # writes=True alone: change, not view


def test_override_without_decorator_removes_the_view():
    class Sub(testadmin.ClientAdmin):
        def names(self, request):
            return "plain method"

        @admin_api.view(name="names2")
        def rest_only(self, request):
            return "renamed"

    views = declared_views(Sub(Client, testadmin.site))
    assert set(views) == {"statement", "hidden", "names2"}


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"params": 42}, "params must be a Form class or a dict"),
        ({"params": {"a": {}}, "required": ["b"]}, r"required \['b'\] not in params"),
        ({"params": forms.Form, "required": ["x"]}, "'required' comes from the form"),
        ({"params": {"pk": {}}, "mcp_tool": "t"}, "clash with the arguments"),
        ({"permissions": []}, "permissions cannot be empty"),
    ],
)
def test_bad_declarations(kwargs, message):
    with pytest.raises(ImproperlyConfigured, match=message):
        admin_api.view(**kwargs)(lambda self, request: None)


# --------------------------------------------------------------------------------------------
# REST: discovery
# --------------------------------------------------------------------------------------------
def test_schema_lists_views(api, data):
    status, body = api("get", f"{CLIENTS}schema/")
    assert status == 200
    views = {v["name"]: v for v in body["views"]}
    assert set(views) == {"statement", "names", "rest_only"}  # hidden: api_enabled=False
    statement = views["statement"]
    assert statement["url"] == "/api/v1/testapp/client/{pk}/views/statement/"
    assert statement["methods"] == ["GET", "POST"]
    assert statement["params_schema"]["properties"]["date_from"] == {
        "type": ["string", "null"],
        "format": "date",
    }
    assert views["names"]["url"] == "/api/v1/testapp/client/views/names/"
    assert views["names"]["params_schema"]["required"] == ["prefix"]

    _status, body = api("get", f"{SALES}schema/")
    post = next(v for v in body["views"] if v["name"] == "post")
    assert post["writes"] is True and post["methods"] == ["POST"] and post["permissions"] == ["change"]


def test_detail_links_detail_views(api, data):
    status, body = api("get", f"{CLIENTS}{data['ahmed'].pk}/")
    assert status == 200
    assert body["views"] == {"statement": f"/api/v1/testapp/client/{data['ahmed'].pk}/views/statement/"}


# --------------------------------------------------------------------------------------------
# REST: running
# --------------------------------------------------------------------------------------------
def test_detail_view_get_with_form_params(api, data):
    url = f"{CLIENTS}{data['ahmed'].pk}/views/statement/?date_from=2025-01-15"
    status, body = api("get", url)
    assert status == 200, body
    assert body == {
        "view": "statement",
        "pk": data["ahmed"].pk,
        "result": {
            "client": {"pk": data["ahmed"].pk, "str": "Ahmed"},
            "from": "2025-01-15",
            "lines": [
                {"slug": "S-2", "date": "2025-02-01", "value": "20.00"},
                {"slug": "S-3", "date": "2025-03-01", "value": "30.00"},
            ],
            "total": "50.00",
            "mcp": False,
        },
    }


def test_detail_view_post_json(api, data):
    status, body = api("post", f"{CLIENTS}{data['ahmed'].pk}/views/statement/", {"status": "draft"})
    assert status == 200 and body["result"]["total"] == "40.00"


def test_form_param_errors(api, data):
    url = f"{CLIENTS}{data['ahmed'].pk}/views/statement/"
    status, body = api("get", f"{url}?date_from=yesterday&bogus=1")
    assert status == 400 and body["error"] == "invalid_params"
    assert body["fields"] == {"bogus": ["Unknown parameter."]}
    status, body = api("post", url, {"date_from": "yesterday", "status": "nope"})
    assert status == 400
    assert set(body["fields"]) == {"date_from", "status"}


def test_collection_view_with_schema_params(api, data):
    status, body = api("get", f"{CLIENTS}views/names/?prefix=A&limit=1&sort=-name&flags=1&flags=2&since=2025-01-02")
    assert status == 200, body
    assert body == {
        "view": "names",
        "result": {"names": ["Amal"], "since": "2025-01-02", "flags": [1, 2]},
    }
    # defaults apply; get_api_queryset (names starting with "A") is honoured
    status, body = api("post", f"{CLIENTS}views/names/", {"prefix": ""})
    assert body["result"] == {"names": ["Ahmed", "Amal"], "since": None, "flags": None}


@pytest.mark.parametrize(
    "payload, fields",
    [
        ({}, {"prefix": ["This parameter is required."]}),
        ({"prefix": 1}, {"prefix": ["Expected string."]}),
        ({"prefix": "A", "limit": 0}, {"limit": ["Must be at least 1."]}),
        ({"prefix": "A", "limit": True}, {"limit": ["Expected integer."]}),
        ({"prefix": "A", "sort": "x"}, {"sort": ["Must be one of: name, -name."]}),
        ({"prefix": "A", "since": "nope"}, {"since": ["Not a valid date."]}),
        ({"prefix": "A", "flags": ["x"]}, {"flags[0]": ["Expected integer."]}),
        ({"prefix": "A", "other": 1}, {"other": ["Unknown parameter."]}),
    ],
)
def test_schema_param_errors(api, data, payload, fields):
    status, body = api("post", f"{CLIENTS}views/names/", payload)
    assert status == 400 and body["error"] == "invalid_params" and body["fields"] == fields


def test_detail_vs_collection_and_unknown(api, data):
    assert api("get", f"{CLIENTS}views/statement/")[0] == 400  # needs a pk
    assert api("get", f"{CLIENTS}{data['ahmed'].pk}/views/names/?prefix=A")[0] == 400  # no pk expected
    assert api("get", f"{CLIENTS}views/hidden/")[0] == 404  # api_enabled=False
    assert api("get", f"{CLIENTS}views/nope/")[0] == 404
    assert api("get", f"{CLIENTS}999/views/statement/")[0] == 404
    bassem = Client.objects.get(name="Bassem")  # outside get_api_queryset
    assert api("get", f"{CLIENTS}{bassem.pk}/views/statement/")[0] == 404


def test_write_view(api, data):
    sale = data["sales"][0]
    url = f"{SALES}{sale.pk}/views/post/"
    status, body = api("get", url)
    assert status == 405 and body["error"] == "method_not_allowed"
    status, body = api("post", url, {"note": "ok"})
    assert status == 200, body
    assert body == {
        "view": "post",
        "pk": sale.pk,
        "result": {"status": "posted", "note": "ok"},
        "messages": [{"level": "info", "text": "S-1 posted."}],
    }
    sale.refresh_from_db()
    assert sale.status == "posted"
    # ValidationError from the view → 400, rolled back
    status, body = api("post", url, {})
    assert status == 400 and body["error"] == "validation_failed" and body["non_field_errors"] == ["Already posted."]


def test_view_returning_a_page_is_rolled_back(api, data):
    status, body = api("post", f"{SALES}views/as_page/", {})
    assert status == 400 and body["error"] == "view_not_supported"
    assert not Sales.objects.filter(status="posted").exclude(slug="S-2").exists()


def test_view_permission_denied_inside(api, data):
    status, body = api("get", f"{SALES}views/forbidden/")
    assert status == 403 and body["detail"] == "not today"


def test_permissions(client, data):
    viewer = staff("view_sales")
    client.force_login(viewer)
    sale = data["sales"][0]
    resp = client.post(f"{SALES}{sale.pk}/views/post/", "{}", content_type="application/json")
    assert resp.status_code == 403  # needs change
    resp = client.post(f"{SALES}views/publish_all/", "{}", content_type="application/json")
    assert resp.status_code == 403  # has_publish_permission: superusers only
    _status_schema = client.get(f"{SALES}schema/").json()
    assert {v["name"] for v in _status_schema["views"]} == {"forbidden"}


def test_token_scopes(superuser, data):
    sale = data["sales"][0]
    viewer = token_client(superuser, "testapp.sales:view")
    assert viewer.get(f"{SALES}views/forbidden/").status_code == 403  # runs, raises inside
    resp = viewer.post(f"{SALES}{sale.pk}/views/post/", "{}", content_type="application/json")
    assert resp.status_code == 403
    # a write view with only a custom permission still needs a write scope
    resp = viewer.post(f"{SALES}views/publish_all/", "{}", content_type="application/json")
    assert resp.status_code == 403
    assert {v["name"] for v in viewer.get(f"{SALES}schema/").json()["views"]} == {"forbidden"}

    changer = token_client(superuser, "testapp.sales:change")
    resp = changer.post(f"{SALES}views/publish_all/", "{}", content_type="application/json")
    assert resp.status_code == 200 and resp.json()["result"] == {"updated": 3}


# --------------------------------------------------------------------------------------------
# nested routing
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "nested, expected",
    [
        ("1/sales/views/totals", (["sales"], ["1"], "view", {"view_name": "totals"})),
        ("1/sales/7/views/lines", (["sales"], ["1"], "object_view", {"object_id": "7", "view_name": "lines"})),
        (
            "1/sales/7/salesitem/9/views/x",
            (["sales", "salesitem"], ["1", "7"], "object_view", {"object_id": "9", "view_name": "x"}),
        ),
        ("1/sales/7", (["sales"], ["1"], "object", {"object_id": "7"})),
    ],
)
def test_parse_nested_view_tails(nested, expected):
    assert parse_nested(nested) == expected


# --------------------------------------------------------------------------------------------
# MCP
# --------------------------------------------------------------------------------------------
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


def test_instructions_mention_views(mcp):
    assert "run_view" in mcp.instructions and "views" in mcp.instructions


def test_list_models_summarizes_views(call, data):
    out, err = call("list_models")
    assert not err
    models = {m["model"]: m for m in out["models"]}
    assert models["testapp.client"]["views"] == [
        {
            "name": "statement",
            "description": "Sales of one client between two dates, with the total.",
            "detail": True,
            "mcp_tool": "client_statement",
        },
        {"name": "names", "description": "Client names starting with a prefix.", "detail": False},
        # rest_only: mcp_enabled=False; hidden: api_enabled=False
    ]
    sales_views = {v["name"]: v for v in models["testapp.sales"]["views"]}
    assert sales_views["post"] == {
        "name": "post",
        "description": "Post a draft sale.",
        "detail": True,
        "writes": True,
        "mcp_tool": "post_sale",
    }


def test_describe_model_gives_params(call, data):
    out, _ = call("describe_model", {"model": "testapp.client"})
    views = {v["name"]: v for v in out["views"]}
    assert set(views) == {"statement", "names"}
    assert views["names"]["params_schema"]["required"] == ["prefix"]
    assert views["statement"]["mcp_tool"] == "client_statement"
    assert "permissions" not in views["statement"]


def test_describe_model_read_only_hides_write_views(call, data):
    out, _ = call("describe_model", {"model": "testapp.sales"}, site=AdminMcpSite(api_site, read_only=True))
    assert {v["name"] for v in out["views"]} == {"forbidden"}


def test_get_object_names_its_views(call, data):
    out, _ = call("get_object", {"model": "testapp.client", "pk": data["ahmed"].pk})
    assert out["views"] == ["statement"]


def test_run_view(call, data):
    out, err = call(
        "run_view",
        {"model": "testapp.client", "view": "statement", "pk": data["ahmed"].pk, "params": {"status": "posted"}},
    )
    assert not err, out
    assert out["result"]["total"] == "20.00" and out["result"]["mcp"] is True
    out, err = call("run_view", {"model": "testapp.client", "view": "names", "params": {"prefix": "Am"}})
    assert not err and out["result"]["names"] == ["Amal"]


def test_run_view_errors(call, data):
    out, err = call("run_view", {"model": "testapp.client", "view": "rest_only"})
    assert err and out["error"] == "not_found"  # mcp_enabled=False
    out, err = call("run_view", {"model": "testapp.client", "view": "names", "params": {}})
    assert err and out["error"] == "invalid_params" and out["status"] == 400
    out, err = call("run_view", {"model": "testapp.client", "view": "names", "params": "x"})
    assert err and out["error"] == "invalid_arguments"


def test_run_view_dry_run_rolls_back(call, data):
    sale = data["sales"][0]
    out, err = call("run_view", {"model": "testapp.sales", "view": "post", "pk": sale.pk, "dry_run": True})
    assert not err, out
    assert out["dry_run"] is True and out["result"]["status"] == "posted"
    assert out["messages"] == [{"level": "info", "text": "S-1 posted."}]
    sale.refresh_from_db()
    assert sale.status == "draft"
    assert testadmin.SAVE_LOG == [("S-1", True)]  # request.is_dry_run seen by the view

    out, err = call("run_view", {"model": "testapp.sales", "view": "post", "pk": sale.pk})
    assert not err
    sale.refresh_from_db()
    assert sale.status == "posted"


def test_run_view_read_only_site(call, data):
    ro = AdminMcpSite(api_site, read_only=True)
    out, err = call("run_view", {"model": "testapp.sales", "view": "post", "pk": data["sales"][0].pk}, site=ro)
    assert err and out["error"] == "not_found"
    out, err = call("run_view", {"model": "testapp.client", "view": "names", "params": {"prefix": "A"}}, site=ro)
    assert not err


def test_promoted_tool(mcp, call, data):
    tool = mcp.get_tool("client_statement")
    assert tool.input_schema["required"] == ["pk"]
    assert set(tool.input_schema["properties"]) == {"pk", "date_from", "date_to", "status"}
    assert tool.annotations["readOnlyHint"] is True
    assert "Sales of one client" in tool.description
    out, err = call("client_statement", {"pk": data["ahmed"].pk, "date_to": "2025-01-31"})
    assert not err and out["result"]["total"] == "10.00"
    out, err = call("client_statement", {"pk": data["ahmed"].pk, "nope": 1})
    assert err and out["fields"] == {"nope": ["Unknown parameter."]}

    post = mcp.get_tool("post_sale")
    assert set(post.input_schema["properties"]) == {"pk", "note", "dry_run"} and post.writes
    sale = data["sales"][0]
    out, err = call("post_sale", {"pk": sale.pk, "dry_run": True})
    assert not err and out["dry_run"] is True
    sale.refresh_from_db()
    assert sale.status == "draft"


def test_promoted_tools_follow_permissions(rf, mcp, data):
    request = rf.post("/mcp/")
    request.user = staff("view_client", "view_sales")
    assert {t.name for t in mcp.get_tools(request)} >= {"client_statement", "run_view"}
    assert "post_sale" not in {t.name for t in mcp.get_tools(request)}  # needs change on sales
    result = mcp.call_tool(request, "post_sale", {"pk": data["sales"][0].pk})
    assert result.is_error and result.data["error"] == "unknown_tool"


def test_promoted_tools_follow_scopes(call, superuser, data):
    out, err = call("post_sale", {"pk": data["sales"][0].pk}, scopes=["testapp.*:view"])
    assert err and out["error"] == "unknown_tool"
    out, err = call("client_statement", {"pk": data["ahmed"].pk}, scopes=["testapp.sales:view"])
    assert err and out["error"] == "unknown_tool"  # no scope on client


def test_view_hook_on_site(call, data):
    class NoStatements(AdminMcpSite):
        def is_view_exposed(self, model_admin, spec):
            return spec.name != "statement"

    site = NoStatements(api_site)
    assert site.get_tool("client_statement") is None
    out, err = call("run_view", {"model": "testapp.client", "view": "statement", "pk": data["ahmed"].pk}, site=site)
    assert err and out["error"] == "not_found"
    out, _ = call("list_models", site=site)
    client = next(m for m in out["models"] if m["model"] == "testapp.client")
    assert [v["name"] for v in client["views"]] == ["names"]


def test_tool_name_clash(db):
    site = admin.AdminSite(name="clash")

    class A(admin.ModelAdmin):
        @admin_api.view(mcp_tool="list_models")
        def x(self, request):
            return 1

    site.register(Client, A)
    mcp = AdminMcpSite(admin_api.AdminApiSite(site, register_token_admin=False))
    with pytest.raises(ImproperlyConfigured, match="'list_models' of view testapp.client:x is already used"):
        mcp.get_tools()


def test_system_check_reports_clashes(monkeypatch):
    from admin_api import checks as admin_api_checks

    class Broken(AdminMcpSite):
        def get_tools(self, request=None):
            raise ImproperlyConfigured("dup")

    broken = Broken(api_site)
    errors = admin_api_checks.check_mcp_view_tools()
    assert [e.id for e in errors] == ["admin_api.E002"] and errors[0].msg == "dup"
    del broken, errors
    assert all(e.id != "admin_api.E002" for e in checks.run_checks(tags=[checks.Tags.urls]))
