"""The SDK-free MCP tool layer, driven through AdminMcpSite.call_tool."""

from decimal import Decimal

import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission

from admin_api.mcp import AdminMcpSite
from admin_api.models import ApiToken
from tests.testapp import admin as testadmin
from tests.testapp.models import Client, Sales, SalesItem
from tests.urls import api_site


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
    sales = [
        Sales.objects.create(slug=f"S-{i}", client=ahmed, value=Decimal(100 * i), status="posted" if i % 2 else "draft")
        for i in range(1, 6)
    ]
    SalesItem.objects.create(sales=sales[0], product="Cheese", quantity=1, price=Decimal(100))
    testadmin.SAVE_LOG.clear()
    testadmin.ON_COMMIT_LOG.clear()
    return {"client": ahmed, "sales": sales}


# --------------------------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------------------------
def test_tools_catalogue(mcp):
    names = [t.name for t in mcp.get_tools()]
    assert names == [
        "list_models",
        "describe_model",
        "list_objects",
        "resolve_entities",
        "get_object",
        "create_object",
        "update_object",
        "delete_object",
        "run_action",
        "run_view",
        "client_statement",  # @admin_api.view(mcp_tool=...)
        "post_sale",
    ]
    ro = AdminMcpSite(api_site, read_only=True)
    assert [t.name for t in ro.get_tools()] == [
        "list_models",
        "describe_model",
        "list_objects",
        "resolve_entities",
        "get_object",
        "run_view",
        "client_statement",  # post_sale writes: hidden
    ]


def test_list_models(call, data):
    out, err = call("list_models")
    assert not err
    models = {m["model"]: m for m in out["models"]}
    assert "testapp.sales" in models and "testapp.client" in models
    assert "testapp.tag" not in models  # mcp_enabled = False
    assert "testapp.secret" not in models  # api_enabled = False
    assert "admin_api.apitoken" not in models
    assert models["testapp.client"]["description"] == "Customers. Sales point to a client."


def test_get_mcp_description_hook(rf, superuser, monkeypatch):
    request = rf.get("/")
    request.user = superuser
    admin = api_site.get_api_admin(testadmin.ClientAdmin(Client, testadmin.site))
    monkeypatch.setattr(type(admin), "get_mcp_description", lambda self, request: "From the hook.")
    assert AdminMcpSite(api_site).get_model_description(admin, request) == "From the hook."


def test_mcp_description_attribute_still_works(rf, superuser):
    request = rf.get("/")
    mcp = AdminMcpSite(api_site)
    plain = type("PlainAdmin", (), {"mcp_description": lambda self, request: "Callable."})()
    assert mcp.get_model_description(plain, request) == "Callable."
    assert mcp.get_model_description(type("NoDescription", (), {})(), request) is None


def test_list_models_read_only_perms(call, data):
    out, _ = call("list_models", site=AdminMcpSite(api_site, read_only=True))
    perms = {m["model"]: m["perms"] for m in out["models"]}["testapp.sales"]
    assert perms == {"add": False, "change": False, "delete": False, "view": True}


def test_describe_model(call, data):
    out, err = call("describe_model", {"model": "testapp.sales"})
    assert not err
    assert out["model"] == "testapp.sales" and out["id_field"] == "id"
    assert {c["name"] for c in out["columns"]} >= {"slug", "status", "doubled"}
    status_filter = next(f for f in out["filters"] if f["title"] == "status")
    assert {"display": "Posted", "params": {"status__exact": "posted"}} in status_filter["choices"]
    assert out["input_schema"]["create"]["required"] == ["slug", "client"]
    assert {a["name"] for a in out["actions"]} >= {"mark_posted", "delete_selected"}
    assert out["per_page"] == {"default": 25, "max": 500}


def test_describe_hidden_model(call, data):
    out, err = call("describe_model", {"model": "testapp.tag"})
    assert err and out["error"] == "not_found"


# --------------------------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------------------------
def test_list_objects_columnar_filters_ordering(call, data):
    out, err = call(
        "list_objects",
        {"model": "testapp.sales", "filters": {"status__exact": "posted"}, "ordering": ["-slug"], "per_page": 2},
    )
    assert not err, out
    assert out["count"] == 3 and out["pages"] == 2 and out["per_page"] == 2
    assert out["columns"][:2] == ["id", "slug"]
    assert [row[1] for row in out["rows"]] == ["S-5", "S-3"]
    assert "next" not in out

    out, _ = call("list_objects", {"model": "testapp.sales", "ordering": "-slug", "per_page": 2, "page": 2})
    assert [row[1] for row in out["rows"]] == ["S-3", "S-2"]


ROW_CAP_HINT = (
    "list_objects pages through the first 3 records of a query. For totals, counts or groupings run a report "
    "(a view under 'views' in list_models); to find records, add filters or search."
)


def test_list_objects_caps_paging_of_a_large_query(call, data, mcp):
    """Past list_max_rows a query reports only the reachable pages, adds a hint, and refuses deeper pages."""
    assert AdminMcpSite.list_max_rows == 1000
    mcp.list_max_rows = 3
    out, err = call("list_objects", {"model": "testapp.sales", "ordering": "slug", "per_page": 2})
    assert not err, out
    assert (out["count"], out["pages"], out["hint"]) == (5, 2, ROW_CAP_HINT)
    out, err = call("list_objects", {"model": "testapp.sales", "ordering": "slug", "per_page": 2, "page": 2})
    assert not err and [row[1] for row in out["rows"]] == ["S-3", "S-4"]
    out, err = call("list_objects", {"model": "testapp.sales", "per_page": 2, "page": 3})
    assert err and out["error"] == "invalid_arguments" and out["detail"] == ROW_CAP_HINT

    mcp.list_max_rows = 4
    out, err = call("list_objects", {"model": "testapp.sales", "per_page": 2, "page": 3})
    assert err and out["error"] == "invalid_arguments"
    out, err = call("list_objects", {"model": "testapp.sales", "per_page": 4})
    assert not err and (out["pages"], len(out["rows"])) == (1, 4)


def test_list_objects_under_the_row_cap_or_without_one_pages_everything(call, data, mcp):
    mcp.list_max_rows = 5
    out, err = call("list_objects", {"model": "testapp.sales", "per_page": 2, "page": 3})
    assert not err and (out["count"], out["pages"], len(out["rows"])) == (5, 3, 1) and "hint" not in out
    mcp.list_max_rows = None
    out, err = call("list_objects", {"model": "testapp.sales", "per_page": 1, "page": 5})
    assert not err and out["pages"] == 5 and "hint" not in out


def test_list_objects_search_and_bad_args(call, data):
    out, _ = call("list_objects", {"model": "testapp.sales", "search": "S-4"})
    assert out["count"] == 1
    out, err = call("list_objects", {"model": "testapp.sales", "ordering": ["badge"]})
    assert err and out["error"] == "invalid_arguments" and "Sortable columns" in out["detail"]
    out, err = call("list_objects", {"model": "testapp.sales", "filters": {"q": "x"}})
    assert err and out["error"] == "invalid_arguments"
    out, err = call("list_objects", {"model": "testapp.sales", "filters": {"nope__exact": 1}})
    assert err and out["error"] == "bad_lookup"
    out, err = call("list_objects", {"model": "sales"})
    assert err and out["error"] == "bad_model"
    out, err = call("list_objects", {})
    assert err and out["error"] == "invalid_arguments"


def test_lookups_on_hidden_fields_are_refused(call, data, monkeypatch):
    """A free-form lookup on a field the API does not show is refused (a prefix oracle on secrets)."""
    Client.objects.filter(pk=data["client"].pk).update(notes="pin 4711")
    monkeypatch.setattr(testadmin.ClientAdmin, "api_fields", ["name"], raising=False)
    out, err = call("list_objects", {"model": "testapp.client", "filters": {"notes__startswith": "pin 4"}})
    assert err and out["error"] == "bad_lookup", out
    out, err = call("list_objects", {"model": "testapp.client", "filters": {"name__startswith": "Ah"}})
    assert not err and out["count"] == 1


def test_lookups_follow_api_lookup_fields(call, data, monkeypatch):
    monkeypatch.setattr(testadmin.ClientAdmin, "api_fields", ["name"], raising=False)
    monkeypatch.setattr(testadmin.ClientAdmin, "api_lookup_fields", ["notes"], raising=False)
    out, err = call("list_objects", {"model": "testapp.client", "filters": {"notes__startswith": "x"}})
    assert not err and out["count"] == 0
    out, err = call("list_objects", {"model": "testapp.client", "filters": {"name__startswith": "Ah"}})
    assert err and out["error"] == "bad_lookup"


def test_lookups_on_change_form_fields_are_allowed(call, data, monkeypatch):
    """Fields shown only on the change form (UserAdmin's add_fieldsets pattern) stay filterable."""
    monkeypatch.setattr(testadmin.ClientAdmin, "api_fields", ["name"], raising=False)
    monkeypatch.setattr(
        testadmin.ClientAdmin,
        "get_fieldsets",
        lambda self, request, obj=None: [(None, {"fields": ["name"] if obj is None else ["name", "notes"]})],
    )
    out, err = call("list_objects", {"model": "testapp.client", "filters": {"notes__startswith": "x"}})
    assert not err and out["count"] == 0


def test_lookups_on_shown_fields_are_allowed(call, data):
    out, err = call("list_objects", {"model": "testapp.sales", "filters": {"value__gte": 300, "slug__startswith": "S"}})
    assert not err and out["count"] == 3


def test_get_object(call, data):
    sale = data["sales"][0]
    out, err = call("get_object", {"model": "testapp.sales", "pk": sale.pk})
    assert not err and out["slug"] == "S-1" and len(out["inlines"]["items"]) == 1
    out, err = call("get_object", {"model": "testapp.sales", "pk": 999})
    assert err and out["error"] == "not_found" and out["status"] == 404


# --------------------------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------------------------
def test_create_and_update(call, data):
    payload = {"slug": "S-9", "client": data["client"].pk, "inlines": {"items": [{"product": "Milk", "price": "7"}]}}
    out, err = call("create_object", {"model": "testapp.sales", "data": payload})
    assert not err, out
    assert out["object"]["slug"] == "S-9" and out["messages"] == [{"level": "info", "text": "Saved S-9"}]
    pk = out["object"]["id"]
    assert LogEntry.objects.filter(object_id=str(pk)).count() == 1

    out, err = call("update_object", {"model": "testapp.sales", "pk": pk, "data": {"status": "posted"}})
    assert not err and out["object"]["status"] == "posted"


def test_validation_error_envelope(call, data):
    out, err = call("create_object", {"model": "testapp.sales", "data": {"slug": "X-1", "client": data["client"].pk}})
    assert err
    assert out["error"] == "validation_failed" and out["status"] == 400
    assert out["non_field_errors"] == ["Slug must start with S-."]


def test_dry_run_create_rolls_back_everything(call, data, django_capture_on_commit_callbacks):
    payload = {"slug": "S-9", "client": data["client"].pk, "inlines": {"items": [{"product": "Milk", "price": "7"}]}}
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        out, err = call("create_object", {"model": "testapp.sales", "data": payload, "dry_run": True})
    assert not err, out
    assert out["dry_run"] is True and out["valid"] is True and out["object"]["slug"] == "S-9"
    assert not Sales.objects.filter(slug="S-9").exists()
    assert not SalesItem.objects.filter(product="Milk").exists()
    assert not LogEntry.objects.exists()
    assert testadmin.SAVE_LOG == [("S-9", True)]  # hooks ran and could see the dry run
    assert callbacks == [] and testadmin.ON_COMMIT_LOG == []  # on_commit side effects discarded


def test_dry_run_invalid_is_not_an_error(call, data):
    out, err = call(
        "create_object",
        {"model": "testapp.sales", "data": {"slug": "X-1", "client": data["client"].pk}, "dry_run": True},
    )
    assert not err
    assert out == {"dry_run": True, "valid": False, "errors": {"non_field_errors": ["Slug must start with S-."]}}


def test_dry_run_update(call, data):
    sale = data["sales"][0]
    out, err = call(
        "update_object", {"model": "testapp.sales", "pk": sale.pk, "data": {"status": "draft"}, "dry_run": True}
    )
    assert not err and out["valid"] and out["object"]["status"] == "draft"
    sale.refresh_from_db()
    assert sale.status == "posted"


def test_delete_needs_confirm(call, data):
    sale = data["sales"][1]
    out, err = call("delete_object", {"model": "testapp.sales", "pk": sale.pk})
    assert not err and out["deleted"] is False and out["confirm_required"] is True
    assert out["preview"]["can_delete"] is True
    assert Sales.objects.filter(pk=sale.pk).exists()
    out, err = call("delete_object", {"model": "testapp.sales", "pk": sale.pk, "confirm": True})
    assert not err and out == {"deleted": True, "object": {"id": sale.pk, "label": "S-2"}}
    assert not Sales.objects.filter(pk=sale.pk).exists()


def test_delete_selected_needs_confirm(call, data, mcp):
    assert mcp.get_tool("run_action").input_schema["properties"]["confirm"]["default"] is False
    args = {"model": "testapp.sales", "action": "delete_selected", "ids": [s.pk for s in data["sales"][:2]]}
    out, err = call("run_action", args)
    assert not err and out["confirm_required"] is True and out["count"] == 2
    assert out["note"] == "Nothing was deleted. Call again with confirm=true to delete."
    assert out["preview"]["model_count"] == {"sales": 2, "sales items": 1} and out["preview"]["can_delete"] is True
    assert Sales.objects.count() == 5
    out, err = call("run_action", {**args, "confirm": True})
    assert not err and out["count"] == 2 and "confirm_required" not in out
    assert sorted(Sales.objects.values_list("slug", flat=True)) == ["S-3", "S-4", "S-5"]


def test_delete_selected_twin_runs_without_preview(call, data, monkeypatch):
    def api_delete_selected(request, queryset):
        return {"kept": queryset.count()}

    monkeypatch.setattr(testadmin.SalesAdmin, "api_delete_selected", staticmethod(api_delete_selected), raising=False)
    args = {"model": "testapp.sales", "action": "delete_selected", "ids": [data["sales"][0].pk]}
    out, err = call("run_action", args)
    assert not err and out["result"] == {"kept": 1} and "confirm_required" not in out


def test_delete_selected_preview_follows_filters(call, data):
    args = {"model": "testapp.sales", "action": "delete_selected", "all": True, "filters": {"status__exact": "draft"}}
    out, err = call("run_action", args)
    assert not err and out["count"] == 2 and out["preview"]["model_count"] == {"sales": 2}
    assert Sales.objects.count() == 5


def test_run_action(call, data):
    drafts = [s.pk for s in data["sales"] if s.status == "draft"]
    out, err = call("run_action", {"model": "testapp.sales", "action": "mark_posted", "pks": drafts})
    assert not err and out["count"] == 2 and out["messages"][0]["text"] == "2 posted."
    assert not Sales.objects.filter(status="draft").exists()


def test_run_action_all_with_filters(call, data):
    out, err = call(
        "run_action",
        {"model": "testapp.sales", "action": "export_csv", "all": True, "filters": {"status__exact": "draft"}},
    )
    assert not err and [r["slug"] for r in out["result"]["rows"]] == ["S-2", "S-4"]


def test_read_only_site_rejects_writes(call, data):
    ro = AdminMcpSite(api_site, read_only=True)
    out, err = call("delete_object", {"model": "testapp.sales", "pk": data["sales"][0].pk, "confirm": True}, site=ro)
    assert err and out["error"] == "unknown_tool"
    out, _ = call("describe_model", {"model": "testapp.sales"}, site=ro)
    assert out["actions"] == [] and out["input_schema"] == {"create": None, "update": None}


# --------------------------------------------------------------------------------------------
# permissions
# --------------------------------------------------------------------------------------------
def test_permissions_and_scopes(call, data, superuser):
    clerk = get_user_model().objects.create_user("clerk", "c@x.io", "pw", is_staff=True)
    clerk.user_permissions.add(Permission.objects.get(codename="view_sales"))
    out, err = call("create_object", {"model": "testapp.sales", "data": {"slug": "S-9"}}, user=clerk)
    assert err and out["error"] == "permission_denied"
    out, _ = call("list_models", user=clerk)
    assert [m["model"] for m in out["models"]] == ["testapp.sales"]

    out, err = call("update_object", {"model": "testapp.sales", "pk": 1, "data": {}}, scopes=["testapp.sales:view"])
    assert err and out["error"] == "permission_denied"
    out, err = call("list_objects", {"model": "testapp.client"}, scopes=["testapp.sales:view"])
    assert err and out["error"] == "permission_denied"


def test_non_staff_rejected(call, data, plain_user):
    out, err = call("list_models", user=plain_user)
    assert err and out["error"] == "permission_denied"


def test_request_flags_and_unexpected_errors(rf, superuser, data):
    from admin_api.mcp import ToolSpec

    seen = {}

    def spy(site, request, args):
        seen.update(is_api=request.is_api, is_mcp=request.is_mcp, is_dry_run=request.is_dry_run)
        raise RuntimeError("boom")

    class SpySite(AdminMcpSite):
        def get_tools(self, request=None):
            return [ToolSpec(name="spy", title="Spy", description="", input_schema={}, handler=spy)]

    request = rf.post("/mcp/")
    request.user = superuser
    result = SpySite(api_site).call_tool(request, "spy", {})
    assert seen == {"is_api": True, "is_mcp": True, "is_dry_run": False}
    assert result.is_error and result.data == {
        "error": "server_error",
        "detail": "The tool failed; see the server log.",
    }


def test_every_call_is_audited_without_payloads(call, data, caplog, superuser):
    caplog.set_level("INFO", logger="admin_api.mcp.audit")
    out, err = call(
        "update_object",
        {"model": "testapp.client", "pk": data["client"].pk, "data": {"name": "s3cret"}, "dry_run": True},
        scopes=["testapp.client:change"],
    )
    call("get_object", {"model": "testapp.sales", "pk": 404})
    first, second = (r.mcp_audit for r in caplog.records if r.name == "admin_api.mcp.audit")
    assert first["tool"] == "update_object" and first["user"] == superuser.pk and first["dry_run"] is True
    assert first["token"] and first["ip"] == "127.0.0.1" and first["model"] == "testapp.client"
    assert not err and first["outcome"] == "ok", out
    assert second["outcome"] == "not_found" and second["pk"] == 404
    assert "s3cret" not in caplog.text


def test_audit_lines_cannot_be_forged_and_name_deletes(call, data, caplog):
    caplog.set_level("INFO", logger="admin_api.mcp.audit")
    call("get_object", {"model": "x\nMCP 'delete_object' user=1", "pk": 1})
    call("delete_object", {"model": "testapp.sales", "pk": data["sales"][0].pk, "confirm": True})
    lines = [r.getMessage() for r in caplog.records if r.name == "admin_api.mcp.audit"]
    assert len(lines) == 2 and "\n" not in lines[0]
    assert caplog.records[-1].mcp_audit["confirm"] is True and "confirm=True" in lines[1]
