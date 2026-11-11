import json
from decimal import Decimal
from io import StringIO

import pytest
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.test import Client as HttpClient

import admin_api
from admin_api import AdminApiSite, services
from admin_api.errors import PermissionDeniedError
from admin_api.models import ApiToken
from admin_api.scopes import normalize_scopes, scopes_allow
from tests.testapp.models import Client, Sales, SalesItem, Tag

SALES = "/api/v1/testapp/sales/"


# --------------------------------------------------------------------------------------------
# grammar
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "scopes, app, model, op, allowed",
    [
        ([], "store", "sales", "delete", True),  # [] = unrestricted
        (["store.sales:view"], "store", "sales", "view", True),
        (["store.sales:view"], "store", "sales", "change", False),
        (["store.sales:change"], "store", "sales", "view", True),  # change implies view, as in Django
        (["store.*:view"], "store", "client", "view", True),
        (["store.*:view"], "cash", "treasury", "view", False),
        (["*.*:view"], "cash", "treasury", "view", True),
        (["cash.*:*"], "cash", "treasury", "delete", True),
        (["store.sales:add"], "store", "sales", "view", False),
        (["garbage"], "store", "sales", "view", False),  # invalid stored scope grants nothing
    ],
)
def test_scopes_allow(scopes, app, model, op, allowed):
    assert scopes_allow(scopes, app, model, op) is allowed


def test_normalize():
    assert normalize_scopes(" Store.Sales:VIEW \n\nstore.sales:view, cash.*:*") == ["store.sales:view", "cash.*:*"]
    assert normalize_scopes(None) == []


@pytest.mark.parametrize("bad", ["store.sales", "store:view", "store.sales:publish", "a.b.c:view", "*:*"])
def test_invalid_scopes(bad):
    with pytest.raises(ValidationError):
        normalize_scopes([bad])


# --------------------------------------------------------------------------------------------
# HTTP matrix
# --------------------------------------------------------------------------------------------
@pytest.fixture
def sale(db):
    ahmed = Client.objects.create(name="Ahmed")
    s = Sales.objects.create(slug="S-1", client=ahmed, value=Decimal(100), status="draft")
    SalesItem.objects.create(sales=s, product="Cheese", quantity=1, price=Decimal(100))
    return s


@pytest.fixture
def call(superuser):
    def make(*scopes):
        _token, raw = ApiToken.objects.create_token(superuser, "t", scopes=list(scopes))
        c = HttpClient(HTTP_AUTHORIZATION=f"Bearer {raw}")

        def do(method, url, payload=None):
            if method == "get":
                resp = c.get(url)
            else:
                resp = getattr(c, method)(url, data=json.dumps(payload or {}), content_type="application/json")
            return resp.status_code, json.loads(resp.content)

        return do

    return make


def test_view_only_token(call, sale):
    api = call("testapp.sales:view")
    assert api("get", SALES)[0] == 200
    assert api("get", f"{SALES}{sale.pk}/")[0] == 200
    assert api("post", SALES, {"slug": "S-2", "client": sale.client_id})[0] == 403
    assert api("patch", f"{SALES}{sale.pk}/", {"status": "posted"})[0] == 403
    assert api("delete", f"{SALES}{sale.pk}/")[0] == 403
    assert api("get", f"{SALES}{sale.pk}/delete-preview/")[0] == 403
    # mark_posted needs 'change': filtered out like any action the user may not run
    assert api("post", f"{SALES}actions/mark_posted/", {"pks": [sale.pk]})[0] == 404
    sale.refresh_from_db()
    assert sale.status == "draft"


def test_registry_reflects_scopes(call, sale):
    status, data = call("testapp.sales:view")("get", "/api/v1/")
    assert status == 200
    models = {m["model"]: m for app in data["apps"] for m in app["models"]}
    assert set(models) == {"sales"}
    assert models["sales"]["perms"] == {"add": False, "change": False, "delete": False, "view": True}


def test_other_model_forbidden(call, sale):
    assert call("testapp.client:view")("get", SALES)[0] == 403


def test_change_scope_runs_change_action(call, sale):
    status, _ = call("testapp.sales:change")("post", f"{SALES}actions/mark_posted/", {"pks": [sale.pk]})
    assert status == 200
    sale.refresh_from_db()
    assert sale.status == "posted"


def test_inline_needs_its_own_scope(call, sale):
    new_row = {"inlines": {"items": [{"product": "Milk", "quantity": 1, "price": "5.00"}]}}

    # no scope at all for the inline model: the inline is not offered (detail omits it, writes reject it)
    api = call("testapp.sales:*")
    status, data = api("get", f"{SALES}{sale.pk}/")
    assert status == 200 and "items" not in data.get("inlines", {})
    assert api("patch", f"{SALES}{sale.pk}/", new_row)[0] == 400

    # view-only on the inline model: rows visible, adding rejected by the formset (max_num = 0)
    api = call("testapp.sales:*", "testapp.salesitem:view")
    status, data = api("get", f"{SALES}{sale.pk}/")
    assert status == 200 and len(data["inlines"]["items"]) == 1
    status, data = api("patch", f"{SALES}{sale.pk}/", new_row)
    assert status == 400
    assert data["inlines"]["items"]["rows"][0]["errors"] == {
        "non_field_errors": ["You may not add rows to this inline."]
    }
    status, data = api(
        "patch", f"{SALES}{sale.pk}/", {"inlines": {"items": [{"pk": sale.items.get().pk, "quantity": 9}]}}
    )
    assert status == 400 and "You may not change rows" in json.dumps(data)
    status, data = api(
        "patch", f"{SALES}{sale.pk}/", {"inlines": {"items": [{"pk": sale.items.get().pk, "_delete": True}]}}
    )
    assert status == 400 and "You may not delete rows" in json.dumps(data)
    assert SalesItem.objects.filter(sales=sale).count() == 1

    # full scope on both: allowed
    status, _ = call("testapp.sales:*", "testapp.salesitem:*")("patch", f"{SALES}{sale.pk}/", new_row)
    assert status == 200
    assert SalesItem.objects.filter(sales=sale).count() == 2


def test_session_is_unrestricted(client, superuser, sale):
    client.force_login(superuser)
    resp = client.patch(f"{SALES}{sale.pk}/", data=json.dumps({"status": "posted"}), content_type="application/json")
    assert resp.status_code == 200


def test_unscoped_token_is_unrestricted(call, sale):
    assert call()("patch", f"{SALES}{sale.pk}/", {"status": "posted"})[0] == 200


# --------------------------------------------------------------------------------------------
# defense in depth: an admin whose has_*_permission ignores super()
# --------------------------------------------------------------------------------------------
class BypassTagAdmin(admin_api.ApiAdminMixin, admin.ModelAdmin):
    def has_view_permission(self, request, obj=None):
        return True

    def has_add_permission(self, request):
        return True

    def has_change_permission(self, request, obj=None):
        return True

    def has_delete_permission(self, request, obj=None):
        return True


def test_services_enforce_scopes_even_if_admin_bypasses_mixin(rf, superuser):
    other = admin.AdminSite(name="bypass")
    other.register(Tag, BypassTagAdmin)
    api_site = AdminApiSite(admin_site=other, name="bypass_api", register_token_admin=False)
    tag = Tag.objects.create(name="t")

    def req(*scopes):
        r = rf.get("/")
        r.user, r.is_api = superuser, True
        r.api_token = ApiToken.objects.create_token(superuser, "t", scopes=list(scopes))[0]
        return r

    with pytest.raises(PermissionDeniedError):
        services.get_model_admin(api_site, req("testapp.client:view"), "testapp", "tag")
    model_admin = services.get_model_admin(api_site, req("testapp.tag:view"), "testapp", "tag")
    with pytest.raises(PermissionDeniedError):
        services.create(model_admin, req("testapp.tag:view"), {"name": "x"})
    with pytest.raises(PermissionDeniedError):
        services.update(model_admin, req("testapp.tag:view"), tag.pk, {"name": "x"}, partial=True)
    with pytest.raises(PermissionDeniedError):
        services.delete(model_admin, req("testapp.tag:view"), tag.pk)
    assert Tag.objects.filter(name="t").exists()


# --------------------------------------------------------------------------------------------
# creating scoped tokens
# --------------------------------------------------------------------------------------------
def test_create_token_validates(superuser):
    token, _ = ApiToken.objects.create_token(superuser, "t", scopes=["Store.Sales:View"])
    assert token.scopes == ["store.sales:view"]
    with pytest.raises(ValidationError):
        ApiToken.objects.create_token(superuser, "t", scopes=["nope"])


def test_command_scopes(superuser):
    out = StringIO()
    call_command("create_api_token", "root", "--scope", "testapp.sales:view", "--scope", "cash.*:*", stdout=out)
    token = ApiToken.objects.get_for_raw(out.getvalue().strip())
    assert token.scopes == ["testapp.sales:view", "cash.*:*"]
    with pytest.raises(CommandError):
        call_command("create_api_token", "root", "--scope", "bad", stdout=StringIO())


def test_admin_form_scopes(client, superuser):
    client.force_login(superuser)
    url = "/admin/admin_api/apitoken/add/"
    resp = client.post(url, {"user": superuser.pk, "name": "ui", "kind": "pat", "scopes_text": "bad scope"})
    assert resp.status_code == 200 and "Invalid scope" in resp.content.decode()
    resp = client.post(
        url, {"user": superuser.pk, "name": "ui", "kind": "pat", "scopes_text": "testapp.sales:view\ncash.*:*"}
    )
    assert resp.status_code == 302
    token = ApiToken.objects.get(name="ui")
    assert token.scopes == ["testapp.sales:view", "cash.*:*"]
    change = client.get(f"/admin/admin_api/apitoken/{token.pk}/change/").content.decode()
    assert "testapp.sales:view" in change
