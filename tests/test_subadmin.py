"""django-subadmin admins through REST and MCP (admin_api.contrib.subadmin).

Client → Sales → SalesItem, see tests/subadmin_site.py. Skipped when django-subadmin is not
installed (or does not support the installed Django).
"""

import json
from decimal import Decimal

import pytest

# ImportError too: a django-subadmin built for another Django version fails to import
pytest.importorskip("subadmin", exc_type=ImportError)

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission

from admin_api.models import ApiToken
from tests import subadmin_site
from tests.testapp.models import Client, Sales, SalesItem

pytestmark = [pytest.mark.django_db, pytest.mark.urls("tests.subadmin_urls")]


@pytest.fixture
def data(db):
    ahmed = Client.objects.create(name="Ahmed")
    bassem = Client.objects.create(name="Bassem")
    s1 = Sales.objects.create(slug="S-1", client=ahmed, value=Decimal(10))
    s2 = Sales.objects.create(slug="S-2", client=ahmed, value=Decimal(20), status="posted")
    s3 = Sales.objects.create(slug="S-3", client=bassem, value=Decimal(30))
    SalesItem.objects.create(sales=s1, product="Milk", quantity=1, price=1)
    SalesItem.objects.create(sales=s1, product="Cheese", quantity=2, price=5)
    SalesItem.objects.create(sales=s3, product="Butter", quantity=1, price=3)
    return {"a": ahmed, "b": bassem, "s1": s1, "s2": s2, "s3": s3}


@pytest.fixture
def api(client, superuser):
    client.force_login(superuser)

    def call(method, url, payload=None, **extra):
        kwargs = {"content_type": "application/json", **extra}
        if payload is not None:
            kwargs["data"] = json.dumps(payload)
        resp = getattr(client, method)(url, **kwargs)
        return resp.status_code, json.loads(resp.content)

    return call


def staff(*codenames):
    user = get_user_model().objects.create_user("staff", "s@x.io", "pw", is_staff=True)
    user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
    return user


def sales_url(d, *rest):
    return f"/api/v1/testapp/client/{d['a'].pk}/sales/" + "".join(f"{r}/" for r in rest)


# --------------------------------------------------------------------------------------------
# REST
# --------------------------------------------------------------------------------------------
def test_registry_lists_nested_admins(api, data):
    status, body = api("get", "/api/v1/")
    assert status == 200
    entries = [m for app in body["apps"] for m in app["models"]]
    by_path = {m.get("path", m["model"]): m for m in entries}
    assert set(by_path) == {"client", "sales", "testapp.client/sales", "testapp.client/sales/salesitem"}
    assert by_path["sales"]["url"] == "/api/v1/testapp/sales/"  # the top-level Sales admin, untouched
    nested = by_path["testapp.client/sales"]
    assert nested["nested_under"] == ["testapp.client"]
    assert nested["url"] is None
    assert nested["url_template"] == "/api/v1/testapp/client/{0}/sales/"
    items = by_path["testapp.client/sales/salesitem"]
    assert items["nested_under"] == ["testapp.client", "testapp.sales"]
    assert items["url_template"] == "/api/v1/testapp/client/{0}/sales/{1}/salesitem/"


def test_changelist_scoped_to_parent(api, data):
    status, body = api("get", sales_url(data))
    assert status == 200
    assert body["count"] == 2
    assert {r["slug"] for r in body["results"]} == {"S-1", "S-2"}
    status, body = api("get", sales_url(data) + "?status__exact=posted")
    assert [r["slug"] for r in body["results"]] == ["S-2"]


def test_two_levels(api, data):
    status, body = api("get", sales_url(data, data["s1"].pk, "salesitem"))
    assert status == 200
    assert {r["product"] for r in body["results"]} == {"Milk", "Cheese"}


def test_inconsistent_chain_is_404(api, data):
    url = f"/api/v1/testapp/client/{data['b'].pk}/sales/{data['s1'].pk}/salesitem/"
    status, body = api("get", url)
    assert status == 404 and body["error"] == "not_found"
    status, _ = api("get", "/api/v1/testapp/client/999/sales/")
    assert status == 404


def test_detail_under_wrong_parent_is_404(api, data):
    status, _ = api("get", sales_url(data, data["s3"].pk))
    assert status == 404


def test_detail_children_links(api, data):
    a, s1 = data["a"], data["s1"]
    status, body = api("get", f"/api/v1/testapp/client/{a.pk}/")
    assert body["children"] == {"sales": f"/api/v1/testapp/client/{a.pk}/sales/"}
    status, body = api("get", sales_url(data, s1.pk))
    assert status == 200 and body["slug"] == "S-1"
    assert "client" not in body  # the parent FK is excluded by subadmin
    assert body["children"] == {"salesitem": f"/api/v1/testapp/client/{a.pk}/sales/{s1.pk}/salesitem/"}
    # the top-level Sales admin has no children
    status, body = api("get", f"/api/v1/testapp/sales/{s1.pk}/")
    assert "children" not in body


def test_schema(api, data):
    status, body = api("get", sales_url(data, "schema"))
    assert status == 200
    assert body["model"]["path"] == "testapp.client/sales"
    assert body["model"]["parents"] == [str(data["a"].pk)]
    assert "client" not in body["input_schema"]["create"]["properties"]
    assert body["children"] == [
        {
            "segment": "salesitem",
            "model": "testapp.client/sales/salesitem",
            "name": "sales item",
            "name_plural": "sales items",
        }
    ]


def test_create_sets_parent(api, data):
    status, body = api("post", sales_url(data), {"slug": "S-9"})
    assert status == 201, body
    assert Sales.objects.get(slug="S-9").client == data["a"]
    status, body = api("post", sales_url(data), {"slug": "S-10", "client": data["b"].pk})
    assert status == 400 and body["error"] == "validation_failed"
    assert not Sales.objects.filter(slug="S-10").exists()


def test_create_two_levels(api, data):
    s1 = data["s1"]
    status, body = api("post", sales_url(data, s1.pk, "salesitem"), {"product": "Yogurt", "quantity": 3})
    assert status == 201, body
    assert SalesItem.objects.get(product="Yogurt").sales == s1


def test_update_delete_preview_delete(api, data):
    s1 = data["s1"]
    status, body = api("patch", sales_url(data, s1.pk), {"value": "99"})
    assert status == 200, body
    assert Sales.objects.get(pk=s1.pk).value == Decimal(99)
    status, body = api("get", sales_url(data, s1.pk, "delete-preview"))
    assert status == 200
    status, body = api("delete", sales_url(data, s1.pk))
    assert status == 200
    assert not Sales.objects.filter(pk=s1.pk).exists()
    # an object of another parent cannot be changed through this path
    status, _ = api("patch", sales_url(data, data["s3"].pk), {"value": "1"})
    assert status == 404


def test_action_only_touches_the_parents_objects(api, data):
    status, body = api("post", sales_url(data, "actions", "delete_selected"), {"all": True})
    assert status == 200, body
    assert body["count"] == 2
    assert list(Sales.objects.values_list("slug", flat=True)) == ["S-3"]


def test_methods_and_unknown_paths(api, data):
    status, _ = api("put", sales_url(data))
    assert status == 405
    status, _ = api("get", f"/api/v1/testapp/client/{data['a'].pk}/nope/")
    assert status == 404
    status, _ = api("get", f"/api/v1/testapp/client/{data['a'].pk}/sales/{data['s1'].pk}/salesitem/x/y/z/")
    assert status == 404


def test_api_enabled_false_hides_the_subadmin_and_its_children(api, data, monkeypatch):
    monkeypatch.setattr(subadmin_site.SalesSub, "api_enabled", False, raising=False)
    assert api("get", sales_url(data))[0] == 404
    assert api("get", sales_url(data, data["s1"].pk, "salesitem"))[0] == 404
    _, body = api("get", "/api/v1/")
    assert not any("path" in m for app in body["apps"] for m in app["models"])
    _, body = api("get", f"/api/v1/testapp/client/{data['a'].pk}/")
    assert "children" not in body


def test_parent_view_permission_required(client, data):
    client.force_login(staff("view_sales"))
    assert client.get(sales_url(data)).status_code == 403
    body = client.get("/api/v1/").json()
    assert [m.get("path", m["model"]) for app in body["apps"] for m in app["models"]] == ["sales"]


def test_read_only_staff(client, data):
    client.force_login(staff("view_client", "view_sales"))
    resp = client.get(sales_url(data))
    assert resp.status_code == 200 and resp.json()["count"] == 2
    resp = client.post(sales_url(data), data="{}", content_type="application/json")
    assert resp.status_code == 403
    body = client.get("/api/v1/").json()
    paths = {m.get("path", m["model"]) for app in body["apps"] for m in app["models"]}
    assert paths == {"client", "sales", "testapp.client/sales"}  # no view_salesitem


def test_token_scopes_need_every_ancestor(client, superuser, data):
    def get(url, scopes):
        _token, raw = ApiToken.objects.create_token(superuser, "t", scopes=scopes)
        return client.get(url, HTTP_AUTHORIZATION=f"Bearer {raw}")

    assert get(sales_url(data), ["testapp.sales:view"]).status_code == 403
    assert get(sales_url(data), ["testapp.client:view", "testapp.sales:view"]).status_code == 200
    resp = get(f"/api/v1/testapp/client/{data['a'].pk}/", ["testapp.client:view"])
    assert "children" not in resp.json()  # no scope on sales: no link


# --------------------------------------------------------------------------------------------
# MCP
# --------------------------------------------------------------------------------------------
@pytest.fixture
def call(rf, superuser):
    def do(name, args=None, *, user=superuser, site=None):
        request = rf.post("/mcp/")
        request.user = user
        result = (site or subadmin_site.mcp_site).call_tool(request, name, args or {})
        return result.data, result.is_error

    return do


def test_mcp_list_models(call, data):
    out, err = call("list_models")
    assert not err
    models = {m["model"]: m for m in out["models"]}
    assert set(models) == {"testapp.client", "testapp.sales", "testapp.client/sales", "testapp.client/sales/salesitem"}
    assert models["testapp.client/sales"]["nested_under"] == ["testapp.client"]
    assert models["testapp.client/sales"]["description"] == "Sales of one client."
    assert models["testapp.client/sales/salesitem"]["nested_under"] == ["testapp.client", "testapp.sales"]
    assert "nested_under" not in models["testapp.sales"]


def test_mcp_describe_model(call, data):
    out, err = call("describe_model", {"model": "testapp.client/sales", "parents": [data["a"].pk]})
    assert not err, out
    assert out["model"] == "testapp.client/sales" and out["parents"] == [data["a"].pk]
    assert "client" not in out["input_schema"]["create"]["properties"]
    assert out["children"] == [
        {"model": "testapp.client/sales/salesitem", "name": "sales item", "name_plural": "sales items"}
    ]
    out, _ = call("describe_model", {"model": "testapp.client"})
    assert out["children"] == [{"model": "testapp.client/sales", "name": "sales", "name_plural": "sales"}]


def test_mcp_parents_are_checked(call, data):
    out, err = call("list_objects", {"model": "testapp.client/sales"})
    assert err and out["error"] == "bad_parents"
    out, err = call("list_objects", {"model": "testapp.client", "parents": [1]})
    assert err and out["error"] == "bad_parents"
    out, err = call(
        "list_objects", {"model": "testapp.client/sales/salesitem", "parents": [data["b"].pk, data["s1"].pk]}
    )
    assert err and out["error"] == "not_found"
    out, err = call("list_objects", {"model": "testapp.client/nope", "parents": [data["a"].pk]})
    assert err and out["error"] == "not_found"
    out, err = call("list_objects", {"model": "testapp.client/sales", "parents": [{"pk": 1}]})
    assert err and out["error"] == "invalid_arguments"


def test_mcp_list_and_get(call, data):
    a, s1 = data["a"], data["s1"]
    out, err = call("list_objects", {"model": "testapp.client/sales", "parents": [a.pk], "ordering": ["slug"]})
    assert not err, out
    assert out["parents"] == [a.pk] and out["count"] == 2
    slug = out["columns"].index("slug")
    assert [row[slug] for row in out["rows"]] == ["S-1", "S-2"]

    out, _ = call("get_object", {"model": "testapp.client", "pk": a.pk})
    assert out["children"] == {"sales": {"model": "testapp.client/sales", "parents": [a.pk]}}
    out, _ = call("get_object", {"model": "testapp.client/sales", "parents": [a.pk], "pk": s1.pk})
    assert out["children"] == {"salesitem": {"model": "testapp.client/sales/salesitem", "parents": [a.pk, s1.pk]}}
    out, err = call("list_objects", out["children"]["salesitem"])
    assert not err and out["count"] == 2


def test_mcp_writes(call, data):
    a, s1 = data["a"], data["s1"]
    args = {"model": "testapp.client/sales", "parents": [a.pk]}
    out, err = call("create_object", {**args, "data": {"slug": "S-9"}, "dry_run": True})
    assert not err and out["valid"], out
    assert not Sales.objects.filter(slug="S-9").exists()
    out, err = call("create_object", {**args, "data": {"slug": "S-9"}})
    assert not err, out
    assert Sales.objects.get(slug="S-9").client == a
    out, err = call("update_object", {**args, "pk": s1.pk, "data": {"value": "5"}})
    assert not err, out
    assert Sales.objects.get(pk=s1.pk).value == Decimal(5)
    out, err = call("update_object", {**args, "pk": data["s3"].pk, "data": {"value": "5"}})
    assert err and out["error"] == "not_found"
    out, _ = call("delete_object", {**args, "pk": s1.pk})
    assert out["confirm_required"]
    out, err = call("delete_object", {**args, "pk": s1.pk, "confirm": True})
    assert not err and out["deleted"]
    out, err = call("run_action", {**args, "action": "delete_selected", "all": True})
    assert not err and out["count"] == 2
    assert list(Sales.objects.values_list("slug", flat=True)) == ["S-3"]


def test_mcp_enabled_false_hides_descendants(call, data, monkeypatch):
    monkeypatch.setattr(subadmin_site.SalesSub, "mcp_enabled", False, raising=False)
    out, _ = call("list_models")
    assert {m["model"] for m in out["models"]} == {"testapp.client", "testapp.sales"}
    out, err = call(
        "list_objects", {"model": "testapp.client/sales/salesitem", "parents": [data["a"].pk, data["s1"].pk]}
    )
    assert err and out["error"] == "not_found"
    out, _ = call("get_object", {"model": "testapp.client", "pk": data["a"].pk})
    assert "children" not in out


def test_mcp_parent_permission(call, data):
    out, err = call(
        "list_objects", {"model": "testapp.client/sales", "parents": [data["a"].pk]}, user=staff("view_sales")
    )
    assert err and out["error"] == "permission_denied"
