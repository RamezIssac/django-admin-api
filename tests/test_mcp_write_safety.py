"""MCP write output (header totals), the require_dry_run confirm token and the "via MCP" LogEntry note."""

import json
from decimal import Decimal

import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import mail
from django.db.models.signals import post_delete, post_save

from admin_api.mcp import AdminMcpSite, confirm
from admin_api.models import ApiToken
from tests.testapp import totals_admin
from tests.testapp.models import Client, Sales, SalesItem
from tests.urls import api_site

ITEMS = {"items": [{"product": "Milk", "quantity": 2, "price": "7"}, {"product": "Tea", "quantity": 1, "price": "16"}]}


@pytest.fixture
def header_signal():
    """Keeps Sales.value in sync from a SalesItem signal, as an ERP's totals signals do."""
    for signal in (post_save, post_delete):
        signal.connect(totals_admin.sync_header_value, sender=SalesItem, dispatch_uid="totals")
    yield
    for signal in (post_save, post_delete):
        signal.disconnect(sender=SalesItem, dispatch_uid="totals")


@pytest.fixture
def ahmed(db):
    return Client.objects.create(name="Ahmed")


@pytest.fixture
def call(rf, superuser):
    def do(site, name, args=None, *, user=superuser, token=None):
        request = rf.post("/mcp/")
        request.user = user
        if token is not None:
            request.api_token = token
        result = site.call_tool(request, name, args or {})
        return result.data, result.is_error

    return do


def create_args(ahmed, **extra):
    return {"model": "testapp.sales", "data": {"slug": "S-9", "client": ahmed.pk, "inlines": ITEMS}, **extra}


# --------------------------------------------------------------------------------------------
# header totals
# --------------------------------------------------------------------------------------------
@pytest.mark.usefixtures("header_signal")
def test_create_returns_totals_written_by_signals(call, ahmed):
    out, err = call(totals_admin.totals_mcp, "create_object", create_args(ahmed))
    assert not err, out
    assert out["totals"] == {"value": "30.00", "line_count": 2}
    assert out["object"]["value"] == "30.00"
    assert "totals" not in out["object"]
    assert Sales.objects.get(slug="S-9").value == Decimal("30.00")


@pytest.mark.usefixtures("header_signal")
def test_dry_run_leaves_totals_to_the_real_write(call, ahmed):
    out, err = call(totals_admin.totals_mcp, "create_object", create_args(ahmed, dry_run=True))
    assert not err and out["valid"] is True and "totals" not in out
    assert not Sales.objects.exists()
    out, err = call(totals_admin.totals_mcp, "create_object", create_args(ahmed))
    assert not err and out["totals"] == {"value": "30.00", "line_count": 2}


@pytest.mark.usefixtures("header_signal")
def test_update_adding_a_row_returns_new_totals(call, ahmed):
    sale = Sales.objects.create(slug="S-1", client=ahmed)
    SalesItem.objects.create(sales=sale, product="Milk", quantity=1, price=Decimal(10))
    args = {"model": "testapp.sales", "pk": sale.pk, "data": {"inlines": {"items": [{"product": "Tea", "price": "5"}]}}}
    out, err = call(totals_admin.totals_mcp, "update_object", args)
    assert not err, out
    assert out["totals"] == {"value": "15.00", "line_count": 2}


def test_totals_are_omitted_when_the_admin_declares_none(call, ahmed):
    out, err = call(AdminMcpSite(api_site), "create_object", create_args(ahmed))
    assert not err and "totals" not in out


def test_rest_write_carries_totals_inside_the_object(rf, superuser, ahmed, header_signal):
    from admin_api import services

    request = rf.post("/api/")
    request.user, request.is_api = superuser, True
    admin_ = totals_admin.totals_api_site.get_model_admin("testapp", "sales")
    out = services.create(admin_, request, {"slug": "S-9", "client": ahmed.pk, "inlines": ITEMS})
    assert out["totals"] == {"value": "30.00", "line_count": 2}


# --------------------------------------------------------------------------------------------
# require_dry_run
# --------------------------------------------------------------------------------------------
@pytest.fixture
def gated():
    return AdminMcpSite(api_site, require_dry_run=True)


def test_real_write_is_refused_without_a_dry_run(call, gated, ahmed):
    out, err = call(gated, "create_object", create_args(ahmed))
    assert err and out["error"] == "dry_run_required" and out["status"] == 409
    assert not Sales.objects.exists()


def test_dry_run_token_unlocks_the_matching_write(call, gated, ahmed):
    dry, err = call(gated, "create_object", create_args(ahmed, dry_run=True))
    assert not err and dry["valid"] and dry["confirm_token"]
    out, err = call(gated, "create_object", create_args(ahmed, confirm_token=dry["confirm_token"]))
    assert not err, out
    assert Sales.objects.filter(slug="S-9").exists()


def test_token_is_bound_to_the_data(call, gated, ahmed):
    dry, _ = call(gated, "create_object", create_args(ahmed, dry_run=True))
    args = create_args(ahmed, confirm_token=dry["confirm_token"])
    args["data"]["slug"] = "S-10"
    out, err = call(gated, "create_object", args)
    assert err and out["error"] == "invalid_confirm_token"
    assert not Sales.objects.exists()


def test_token_is_single_use(call, gated, ahmed):
    dry, _ = call(gated, "create_object", create_args(ahmed, dry_run=True))
    out, err = call(gated, "create_object", create_args(ahmed, confirm_token=dry["confirm_token"]))
    assert not err, out
    out, err = call(gated, "create_object", create_args(ahmed, confirm_token=dry["confirm_token"]))
    assert err and out["error"] == "invalid_confirm_token" and "already used" in out["detail"]
    assert Sales.objects.count() == 1


def test_replayed_update_and_view_are_refused(call, gated, ahmed):
    sale = Sales.objects.create(slug="S-1", client=ahmed)
    args = {"model": "testapp.sales", "pk": sale.pk, "data": {"value": "5"}}
    dry, _ = call(gated, "update_object", {**args, "dry_run": True})
    assert not call(gated, "update_object", {**args, "confirm_token": dry["confirm_token"]})[1]
    out, err = call(gated, "update_object", {**args, "confirm_token": dry["confirm_token"]})
    assert err and out["error"] == "invalid_confirm_token"

    view_args = {"pk": sale.pk, "note": "x"}
    dry, _ = call(gated, "post_sale", {**view_args, "dry_run": True})
    assert not call(gated, "post_sale", {**view_args, "confirm_token": dry["confirm_token"]})[1]
    Sales.objects.filter(pk=sale.pk).update(status="draft")  # back to the dry run's state
    run_view = {"model": "testapp.sales", "view": "post", "pk": sale.pk, "params": {"note": "x"}}
    out, err = call(gated, "run_view", {**run_view, "confirm_token": dry["confirm_token"]})
    assert err and out["error"] == "invalid_confirm_token"


def test_a_mismatched_write_does_not_spend_the_token(call, gated, ahmed):
    dry, _ = call(gated, "create_object", create_args(ahmed, dry_run=True))
    other = create_args(ahmed, confirm_token=dry["confirm_token"])
    other["data"]["slug"] = "S-10"
    assert call(gated, "create_object", other)[1]
    out, err = call(gated, "create_object", create_args(ahmed, confirm_token=dry["confirm_token"]))
    assert not err, out


def test_token_is_bound_to_the_object_state(call, gated, ahmed):
    sale = Sales.objects.create(slug="S-1", client=ahmed)
    args = {"model": "testapp.sales", "pk": sale.pk, "data": {"status": "posted"}}
    dry, _ = call(gated, "update_object", {**args, "dry_run": True})
    Sales.objects.filter(pk=sale.pk).update(value=Decimal(99))  # a field the write does not send
    out, err = call(gated, "update_object", {**args, "confirm_token": dry["confirm_token"]})
    assert err and out["error"] == "invalid_confirm_token" and "changed" in out["detail"]
    sale.refresh_from_db()
    assert sale.status == "draft"


def test_view_token_is_bound_to_the_object_state(call, gated, ahmed):
    sale = Sales.objects.create(slug="S-1", client=ahmed)
    args = {"pk": sale.pk, "note": "x"}
    dry, _ = call(gated, "post_sale", {**args, "dry_run": True})
    Sales.objects.filter(pk=sale.pk).update(slug="S-1b")
    out, err = call(gated, "post_sale", {**args, "confirm_token": dry["confirm_token"]})
    assert err and out["error"] == "invalid_confirm_token"
    sale.refresh_from_db()
    assert sale.status == "draft"


@pytest.fixture
def item_log(monkeypatch):
    """The admin pins a sale's items, state stored outside the sale's own row."""
    seen = []

    def get_mcp_confirm_state(self, request, obj):
        seen.append(obj)
        return sorted(obj.items.values_list("pk", flat=True)) if obj else None

    sales_admin = api_site.get_model_admin("testapp", "sales")
    monkeypatch.setattr(type(sales_admin), "get_mcp_confirm_state", get_mcp_confirm_state, raising=False)
    return seen


def test_confirm_state_hook_catches_a_change_outside_the_row(call, gated, ahmed, item_log):
    sale = Sales.objects.create(slug="S-1", client=ahmed)
    args = {"pk": sale.pk, "note": "x"}
    dry, _ = call(gated, "post_sale", {**args, "dry_run": True})
    SalesItem.objects.create(sales=sale, product="Milk")  # a concurrent write to another table
    out, err = call(gated, "post_sale", {**args, "confirm_token": dry["confirm_token"]})
    assert err and out["error"] == "invalid_confirm_token" and out["status"] == 409
    sale.refresh_from_db()
    assert sale.status == "draft"


def test_confirm_state_hook_accepts_an_unchanged_write(call, gated, ahmed, item_log):
    sale = Sales.objects.create(slug="S-1", client=ahmed)
    SalesItem.objects.create(sales=sale, product="Milk")
    args = {"pk": sale.pk, "note": "x"}
    dry, _ = call(gated, "post_sale", {**args, "dry_run": True})
    out, err = call(gated, "post_sale", {**args, "confirm_token": dry["confirm_token"]})
    assert not err, out
    dry, _ = call(gated, "create_object", create_args(ahmed, dry_run=True))
    assert not call(gated, "create_object", create_args(ahmed, confirm_token=dry["confirm_token"]))[1]
    assert item_log[-2:] == [None, None]  # a create passes no object


def test_write_state_pins_the_row_and_the_hook_value(rf, superuser, ahmed, item_log):
    sale = Sales.objects.create(slug="S-1", client=ahmed)
    item = SalesItem.objects.create(sales=sale, product="Milk")
    request = rf.post("/mcp/")
    request.user = superuser
    state = confirm.write_state(api_site.get_model_admin("testapp", "sales"), request, sale)
    assert state == {"row": Sales.objects.filter(pk=sale.pk).values().first(), "extra": [item.pk]}


def test_confirm_state_hook_gets_the_request(call, gated, ahmed, superuser, monkeypatch):
    users = []

    def get_mcp_confirm_state(self, request, obj):
        users.append(request.user.username)

    sales_admin = api_site.get_model_admin("testapp", "sales")
    monkeypatch.setattr(type(sales_admin), "get_mcp_confirm_state", get_mcp_confirm_state, raising=False)
    sale = Sales.objects.create(slug="S-1", client=ahmed)
    view = {"pk": sale.pk, "note": "x"}
    dry, _ = call(gated, "post_sale", {**view, "dry_run": True})
    assert not call(gated, "post_sale", {**view, "confirm_token": dry["confirm_token"]})[1]
    update = {"model": "testapp.sales", "pk": sale.pk, "data": {"value": "3"}}
    dry, _ = call(gated, "update_object", {**update, "dry_run": True})
    assert not call(gated, "update_object", {**update, "confirm_token": dry["confirm_token"]})[1]
    assert users == [superuser.username] * 4


def test_token_is_bound_to_the_api_token(call, gated, ahmed, superuser):
    token_a = ApiToken.objects.create_token(superuser, "a", kind="mcp")[0]
    token_b = ApiToken.objects.create_token(superuser, "b", kind="mcp")[0]
    dry, _ = call(gated, "create_object", create_args(ahmed, dry_run=True), token=token_a)
    out, err = call(gated, "create_object", create_args(ahmed, confirm_token=dry["confirm_token"]), token=token_b)
    assert err and out["error"] == "invalid_confirm_token"
    out, err = call(gated, "create_object", create_args(ahmed, confirm_token=dry["confirm_token"]), token=token_a)
    assert not err, out


@pytest.fixture
def sales_viewer(db):
    user = get_user_model().objects.create_user("viewer", "v@x.io", "pw", is_staff=True)
    user.user_permissions.add(*Permission.objects.filter(codename__in=["view_sales", "view_client"]))
    return user


def test_permission_is_checked_before_the_confirm_token(call, gated, ahmed, sales_viewer):
    """A user who may not write gets 403, not the 409 that invites a dry run."""
    sale = Sales.objects.create(slug="S-1", client=ahmed)
    cases = [
        ("create_object", create_args(ahmed)),
        ("update_object", {"model": "testapp.sales", "pk": sale.pk, "data": {"status": "posted"}}),
        ("run_view", {"model": "testapp.sales", "view": "post", "pk": sale.pk, "params": {"note": "x"}}),
    ]
    for name, args in cases:
        for extra in ({}, {"confirm_token": "bogus"}):
            out, err = call(gated, name, {**args, **extra}, user=sales_viewer)
            assert err and out["status"] == 403, (name, extra, out)


def test_token_scope_is_checked_before_the_confirm_token(call, gated, ahmed, superuser):
    view_only = ApiToken.objects.create_token(superuser, "ro", scopes=["testapp.*:view"])[0]
    out, err = call(gated, "create_object", create_args(ahmed), token=view_only)
    assert err and out["status"] == 403


def test_a_short_lived_site_does_not_expire_another_sites_tokens(call, gated, ahmed):
    dry, _ = call(gated, "create_object", create_args(ahmed, dry_run=True))
    short = AdminMcpSite(api_site, name="short", require_dry_run=True)
    short.confirm_token_max_age = 60
    call(short, "create_object", create_args(ahmed, dry_run=True))
    out, err = call(gated, "create_object", create_args(ahmed, confirm_token=dry["confirm_token"]))
    assert not err, out


def test_binary_state_hashes_the_same_twice(rf, superuser):
    from admin_api.mcp.confirm import fingerprint

    request = rf.post("/mcp/")
    request.user = superuser
    as_bytes = fingerprint(request, "op", {}, {"blob": b"\x01\xff"})
    assert fingerprint(request, "op", {}, {"blob": memoryview(b"\x01\xff")}) == as_bytes


def test_a_failed_write_leaves_the_token_unspent(call, gated, ahmed):
    from admin_api.models import McpConfirmation

    other = Client.objects.create(name="Basma")
    sale = Sales.objects.create(slug="S-1", client=ahmed)
    args = {"model": "testapp.sales", "pk": sale.pk, "data": {"client": other.pk}}
    dry, _ = call(gated, "update_object", {**args, "dry_run": True})
    other.delete()  # the real write now fails validation; the sale's row is unchanged
    out, err = call(gated, "update_object", {**args, "confirm_token": dry["confirm_token"]})
    assert err and out["error"] == "validation_failed", out
    assert McpConfirmation.objects.get().used_at is None


def test_token_is_bound_to_the_user(call, gated, ahmed):
    dry, _ = call(gated, "create_object", create_args(ahmed, dry_run=True))
    other = get_user_model().objects.create_superuser("other", "o@x.io", "pw")
    out, err = call(gated, "create_object", create_args(ahmed, confirm_token=dry["confirm_token"]), user=other)
    assert err and out["error"] == "invalid_confirm_token"


def test_token_is_bound_to_the_site(call, gated, ahmed):
    dry, _ = call(gated, "create_object", create_args(ahmed, dry_run=True))
    other_site = AdminMcpSite(api_site, name="other", require_dry_run=True)
    out, err = call(other_site, "create_object", create_args(ahmed, confirm_token=dry["confirm_token"]))
    assert err and out["error"] == "invalid_confirm_token"


def test_expired_and_forged_tokens_are_refused(call, gated, ahmed):
    dry, _ = call(gated, "create_object", create_args(ahmed, dry_run=True))
    gated.confirm_token_max_age = -1
    out, err = call(gated, "create_object", create_args(ahmed, confirm_token=dry["confirm_token"]))
    assert err and out["error"] == "invalid_confirm_token" and "expired" in out["detail"]
    gated.confirm_token_max_age = 900
    out, err = call(gated, "create_object", create_args(ahmed, confirm_token="not-a-token"))
    assert err and out["error"] == "invalid_confirm_token"


def test_invalid_dry_run_issues_no_token(call, gated, ahmed):
    args = create_args(ahmed, dry_run=True)
    args["data"]["slug"] = "X-1"
    out, err = call(gated, "create_object", args)
    assert not err and out["valid"] is False and "confirm_token" not in out


def test_update_is_gated_too(call, gated, ahmed):
    sale = Sales.objects.create(slug="S-1", client=ahmed)
    args = {"model": "testapp.sales", "pk": sale.pk, "data": {"status": "posted"}}
    out, err = call(gated, "update_object", args)
    assert err and out["error"] == "dry_run_required"
    dry, _ = call(gated, "update_object", {**args, "dry_run": True})
    out, err = call(gated, "update_object", {**args, "confirm_token": dry["confirm_token"]})
    assert not err and out["object"]["status"] == "posted"


def test_write_views_are_gated(call, gated, ahmed):
    sale = Sales.objects.create(slug="S-1", client=ahmed)
    args = {"pk": sale.pk, "note": "x"}
    out, err = call(gated, "post_sale", args)
    assert err and out["error"] == "dry_run_required"
    dry, _ = call(gated, "post_sale", {**args, "dry_run": True})
    assert dry["confirm_token"]
    out, err = call(gated, "post_sale", {**args, "confirm_token": dry["confirm_token"]})
    assert not err
    sale.refresh_from_db()
    assert sale.status == "posted"


def test_delete_and_read_tools_are_not_gated(call, gated, ahmed):
    out, err = call(gated, "list_objects", {"model": "testapp.client"})
    assert not err
    sale = Sales.objects.create(slug="S-1", client=ahmed)
    out, err = call(gated, "delete_object", {"model": "testapp.sales", "pk": sale.pk, "confirm": True})
    assert not err and out["deleted"] is True


def test_default_site_needs_no_token_and_issues_none(call, ahmed):
    site = AdminMcpSite(api_site)
    dry, _ = call(site, "create_object", create_args(ahmed, dry_run=True))
    assert "confirm_token" not in dry
    _out, err = call(site, "create_object", create_args(ahmed))
    assert not err


def test_admin_override_beats_the_site_setting(call, gated, ahmed, monkeypatch):
    sales_admin = api_site.get_model_admin("testapp", "sales")
    monkeypatch.setattr(type(sales_admin), "mcp_require_dry_run", False, raising=False)
    out, err = call(gated, "create_object", create_args(ahmed))
    assert not err, out
    monkeypatch.setattr(type(sales_admin), "mcp_require_dry_run", True)
    out, err = call(AdminMcpSite(api_site), "update_object", {"model": "testapp.sales", "pk": 1, "data": {}})
    assert err and out["error"] == "dry_run_required"


def test_token_flow_over_stateless_http(client, superuser, ahmed, monkeypatch):
    from tests.test_mcp_http import call_tool
    from tests.urls import mcp_site

    monkeypatch.setattr(mcp_site, "require_dry_run", True)
    token = ApiToken.objects.create_token(superuser, "mcp", kind="mcp")[1]
    out, err = call_tool(client, token, "create_object", create_args(ahmed))
    assert err and out["error"] == "dry_run_required"
    dry, err = call_tool(client, token, "create_object", create_args(ahmed, dry_run=True))
    assert not err and dry["confirm_token"]
    out, err = call_tool(client, token, "create_object", create_args(ahmed, confirm_token=dry["confirm_token"]))
    assert not err and Sales.objects.filter(slug="S-9").exists()


# --------------------------------------------------------------------------------------------
# a dry run validates only: no save, no signal, no view call
# --------------------------------------------------------------------------------------------
@pytest.fixture
def mail_on_post():
    """Mails the client from a post_save signal once a sale is posted."""

    def notify(sender, instance, **kwargs):
        if instance.status == "posted":
            mail.send_mail(f"{instance.slug} posted", "", "erp@example.com", ["client@example.com"])

    post_save.connect(notify, sender=Sales, dispatch_uid="mail_on_post")
    yield
    post_save.disconnect(sender=Sales, dispatch_uid="mail_on_post")


@pytest.fixture
def mail_on_save_model(monkeypatch):
    """Mails from the admin's save_model, a side effect outside on_commit."""
    sales_admin = type(api_site.get_model_admin("testapp", "sales"))
    save_model = sales_admin.save_model

    def mailing_save_model(self, request, obj, form, change):
        mail.send_mail(f"{obj.slug} saved", "", "erp@example.com", ["boss@example.com"])
        save_model(self, request, obj, form, change)

    monkeypatch.setattr(sales_admin, "save_model", mailing_save_model)


@pytest.mark.usefixtures("mail_on_post", "mail_on_save_model")
def test_update_dry_run_runs_no_save_model_and_no_signal(call, ahmed):
    site = AdminMcpSite(api_site)
    sale = Sales.objects.create(slug="S-1", client=ahmed)
    args = {"model": "testapp.sales", "pk": sale.pk, "data": {"status": "posted"}}
    out, err = call(site, "update_object", {**args, "dry_run": True})
    assert not err and out == {
        "dry_run": True,
        "valid": True,
        "note": "Valid. Nothing was saved; totals come with the real write.",
    }
    assert mail.outbox == [] and not LogEntry.objects.exists()
    sale.refresh_from_db()
    assert sale.status == "draft"

    out, err = call(site, "update_object", args)
    assert not err, out
    assert [m.subject for m in mail.outbox] == ["S-1 saved", "S-1 posted"]


@pytest.mark.usefixtures("mail_on_post")
def test_view_dry_run_does_not_run_the_view(call, ahmed):
    site = AdminMcpSite(api_site)
    sale = Sales.objects.create(slug="S-1", client=ahmed)
    run_view = {"model": "testapp.sales", "view": "post", "pk": sale.pk, "params": {}, "dry_run": True}
    out, err = call(site, "run_view", run_view)
    assert not err and out == {
        "dry_run": True,
        "valid": True,
        "note": "Valid. The view did not run; nothing was saved.",
    }
    assert mail.outbox == []
    sale.refresh_from_db()
    assert sale.status == "draft"

    out, err = call(site, "run_view", {**run_view, "dry_run": False})
    assert not err and [m.subject for m in mail.outbox] == ["S-1 posted"]


def test_view_dry_run_validates_the_params(call, ahmed):
    sale = Sales.objects.create(slug="S-1", client=ahmed)
    args = {"model": "testapp.sales", "view": "post", "pk": sale.pk, "params": {"note": 5}, "dry_run": True}
    out, err = call(AdminMcpSite(api_site), "run_view", args)
    assert not err and out == {"dry_run": True, "valid": False, "errors": {"fields": {"note": ["Expected string."]}}}


def test_admin_validate_changeform_replaces_the_default_on_a_dry_run(call, ahmed, monkeypatch):
    from admin_api.writes import validate_changeform

    def whole_document_check(self, request, obj, change):
        result = validate_changeform(self, request, obj, change)
        result.form.add_error(None, "The client is on hold.")
        result.valid = False
        return result

    monkeypatch.setattr(
        type(api_site.get_model_admin("testapp", "sales")), "validate_changeform", whole_document_check, raising=False
    )
    out, err = call(AdminMcpSite(api_site), "create_object", create_args(ahmed, dry_run=True))
    assert not err and out["valid"] is False
    assert out["errors"] == {"non_field_errors": ["The client is on hold."]}


def test_dry_run_reports_form_and_inline_errors(call, ahmed):
    data = {"slug": "X-1", "client": ahmed.pk, "inlines": {"items": [{"product": "Milk", "price": "abc"}]}}
    out, err = call(AdminMcpSite(api_site), "create_object", {"model": "testapp.sales", "data": data, "dry_run": True})
    assert not err and out["dry_run"] is True and out["valid"] is False
    assert out["errors"]["non_field_errors"] == ["Slug must start with S-."]
    assert out["errors"]["inlines"]["items"]["rows"][0]["errors"] == {"price": ["Enter a number."]}
    assert not Sales.objects.exists()


def test_dry_run_checks_permissions_and_the_admin_queryset(call, ahmed):
    site = AdminMcpSite(api_site)
    hidden = Sales.objects.create(slug="HIDDEN", client=ahmed)
    out, err = call(site, "update_object", {"model": "testapp.sales", "pk": hidden.pk, "data": {}, "dry_run": True})
    assert err and out["error"] == "not_found"
    viewer = get_user_model().objects.create_user("viewer", "v@x.io", "pw", is_staff=True)
    viewer.user_permissions.add(Permission.objects.get(codename="view_sales"))
    sale = Sales.objects.create(slug="S-1", client=ahmed)
    args = {"model": "testapp.sales", "pk": sale.pk, "data": {}, "dry_run": True}
    out, err = call(site, "update_object", args, user=viewer)
    assert err and out["error"] == "permission_denied"


def test_validation_sees_is_dry_run(call, ahmed, monkeypatch):
    sales_admin = type(api_site.get_model_admin("testapp", "sales"))
    get_form = sales_admin.get_form
    seen = []

    def recording_get_form(self, request, *args, **kwargs):
        seen.append(request.is_dry_run)
        return get_form(self, request, *args, **kwargs)

    monkeypatch.setattr(sales_admin, "get_form", recording_get_form)
    request_args = create_args(ahmed, dry_run=True)
    assert not call(AdminMcpSite(api_site), "create_object", request_args)[1]
    assert seen and all(seen)
    seen.clear()
    assert not call(AdminMcpSite(api_site), "create_object", {**request_args, "dry_run": False})[1]
    assert seen and not any(seen)


# --------------------------------------------------------------------------------------------
# LogEntry
# --------------------------------------------------------------------------------------------
def test_mcp_log_entries_say_via_mcp(call, ahmed):
    site = AdminMcpSite(api_site)
    out, _ = call(site, "create_object", create_args(ahmed))
    pk = out["object"]["id"]
    added = LogEntry.objects.get(object_id=str(pk))
    assert added.get_change_message().endswith("Via MCP.")
    assert added.get_change_message().startswith("Added")

    call(site, "update_object", {"model": "testapp.sales", "pk": pk, "data": {"status": "posted"}})
    changed = LogEntry.objects.filter(object_id=str(pk)).latest("id")
    assert changed.get_change_message() == "Changed Status. Via MCP."


def test_rest_log_entries_keep_the_structured_message(client, superuser, ahmed):
    client.force_login(superuser)
    body = json.dumps({"slug": "S-9", "client": ahmed.pk})
    resp = client.post("/api/v1/testapp/sales/", data=body, content_type="application/json")
    assert resp.status_code == 201
    entry = LogEntry.objects.get(object_id=str(json.loads(resp.content)["pk"]))
    assert json.loads(entry.change_message) == [{"added": {}}]


@pytest.mark.parametrize("slug", ["S-9", "X-1"])  # valid and invalid dry runs
def test_is_dry_run_is_false_again_after_a_dry_run(rf, superuser, ahmed, slug):
    request = rf.post("/mcp/")
    request.user = superuser
    args = {"model": "testapp.sales", "data": {"slug": slug, "client": ahmed.pk}, "dry_run": True}
    assert not AdminMcpSite(api_site).call_tool(request, "create_object", args).is_error
    assert request.is_dry_run is False
