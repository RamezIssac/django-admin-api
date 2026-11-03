import json
from contextlib import contextmanager
from decimal import Decimal

import pytest
from django.contrib.admin.models import ADDITION, CHANGE, LogEntry

from tests.testapp.models import Client, Sales, SalesItem, Tag

LIST = "/api/v1/testapp/sales/"


@pytest.fixture
def ahmed(db):
    return Client.objects.create(name="Ahmed")


@pytest.fixture
def sale(ahmed):
    s = Sales.objects.create(slug="S-1", client=ahmed, value=Decimal(200), status="posted")
    SalesItem.objects.create(sales=s, product="Cheese", quantity=1, price=Decimal(100))
    SalesItem.objects.create(sales=s, product="Milk", quantity=1, price=Decimal(100))
    return s


@pytest.fixture
def api(client, superuser):
    client.force_login(superuser)

    def call(method, url, payload=None, raw=None):
        body = raw if raw is not None else json.dumps(payload or {})
        resp = getattr(client, method)(url, data=body, content_type="application/json")
        return resp.status_code, json.loads(resp.content)

    call.client = client
    return call


def obj_url(obj):
    return f"{LIST}{obj.pk}/"


@pytest.mark.django_db
class TestCreate:
    def test_create_with_inline_rows(self, api, ahmed):
        status, body = api(
            "post",
            LIST,
            {
                "slug": "S-9",
                "client": ahmed.pk,
                "inlines": {"items": [{"product": "Cheese", "quantity": 2, "price": "10"}]},
            },
        )
        assert status == 201, body
        sale = Sales.objects.get(slug="S-9")
        assert body["pk"] == sale.pk
        assert body["value"] == "20.00"  # recomputed by the admin's save_related
        assert body["status"] == "draft"  # model default for a missing field
        assert [(i["product"], i["quantity"]) for i in body["inlines"]["items"]] == [("Cheese", 2)]
        assert body["messages"] == [{"level": "info", "text": "Saved S-9"}]
        entry = LogEntry.objects.get(object_id=str(sale.pk))
        assert entry.action_flag == ADDITION

    def test_messages_do_not_leak_into_the_session(self, api, ahmed):
        assert api("post", LIST, {"slug": "S-9", "client": ahmed.pk})[0] == 201
        cookie = api.client.cookies.get("messages")
        assert cookie is None or not cookie.value
        assert "_messages" not in api.client.session

    def test_required_field(self, api, db):
        status, body = api("post", LIST, {"slug": "S-9"})
        assert status == 400 and body["error"] == "validation_failed"
        assert body["fields"] == {"client": ["This field is required."]}

    def test_form_clean_non_field_error(self, api, ahmed):
        status, body = api("post", LIST, {"slug": "X-1", "client": ahmed.pk})
        assert status == 400 and body["non_field_errors"] == ["Slug must start with S-."]
        assert not Sales.objects.exists()

    def test_unknown_and_readonly_keys_rejected(self, api, ahmed):
        status, body = api("post", LIST, {"slug": "S-9", "client": ahmed.pk, "nope": 1, "doubled": 2})
        assert status == 400
        assert set(body["fields"]) == {"nope", "doubled"}

    def test_fk_as_object_and_m2m(self, api, ahmed):
        vip, gold = Tag.objects.create(name="vip"), Tag.objects.create(name="gold")
        status, body = api(
            "post", LIST, {"slug": "S-9", "client": {"pk": ahmed.pk, "str": "Ahmed"}, "tags": [vip.pk, gold.pk]}
        )
        assert status == 201, body
        assert {t["str"] for t in body["tags"]} == {"vip", "gold"}

    def test_inline_row_error_rolls_back_everything(self, api, ahmed):
        status, body = api(
            "post",
            LIST,
            {"slug": "S-9", "client": ahmed.pk, "inlines": {"items": [{"product": "Cheese", "quantity": -1}]}},
        )
        assert status == 400
        rows = body["inlines"]["items"]["rows"]
        assert rows[0]["index"] == 0 and "quantity" in rows[0]["errors"]
        assert not Sales.objects.exists() and not SalesItem.objects.exists()

    def test_invalid_json(self, api, db):
        status, body = api("post", LIST, raw="{nope")
        assert status == 400 and body["error"] == "invalid_json"
        assert api("post", LIST, raw="[1, 2]")[0] == 400

    def test_add_permission(self, client, staff_view_clients, ahmed):
        client.force_login(staff_view_clients)
        resp = client.post("/api/v1/testapp/client/", data="{}", content_type="application/json")
        assert resp.status_code == 403


@pytest.mark.django_db
class TestUpdate:
    def test_patch_keeps_missing_fields(self, api, sale):
        status, body = api("patch", obj_url(sale), {"slug": "S-1b"})
        assert status == 200, body
        sale.refresh_from_db()
        assert (sale.slug, sale.status, sale.client.name) == ("S-1b", "posted", "Ahmed")
        assert SalesItem.objects.filter(sales=sale).count() == 2  # untouched inline rows kept
        entry = LogEntry.objects.get(object_id=str(sale.pk))
        assert entry.action_flag == CHANGE and "Slug" in entry.get_change_message()

    def test_put_resets_missing_fields_to_defaults(self, api, sale):
        status, body = api("put", obj_url(sale), {"slug": "S-1", "client": sale.client.pk})
        assert status == 200, body
        sale.refresh_from_db()
        assert sale.status == "draft"

    def test_inline_update_delete_add(self, api, sale):
        cheese, milk = SalesItem.objects.filter(sales=sale).order_by("pk")
        status, body = api(
            "patch",
            obj_url(sale),
            {
                "inlines": {
                    "items": [
                        {"pk": cheese.pk, "quantity": 5},
                        {"pk": milk.pk, "_delete": True},
                        {"product": "Butter", "quantity": 1, "price": "50"},
                    ]
                }
            },
        )
        assert status == 200, body
        rows = {r["product"]: r["quantity"] for r in body["inlines"]["items"]}
        assert rows == {"Cheese": 5, "Butter": 1}
        assert body["value"] == "550.00"  # 5*100 + 1*50, via save_related

    def test_unknown_inline_row_pk(self, api, sale):
        status, body = api("patch", obj_url(sale), {"inlines": {"items": [{"pk": 9999, "quantity": 1}]}})
        assert status == 400 and body["inlines"]["items"]["rows"][0]["errors"] == {
            "pk": ["No such row in this inline."]
        }

    def test_unknown_inline(self, api, sale):
        status, body = api("patch", obj_url(sale), {"inlines": {"nope": []}})
        assert status == 400 and "nope" in body["inlines"]

    def test_change_permission(self, client, staff_view_clients, ahmed):
        client.force_login(staff_view_clients)
        resp = client.patch(f"/api/v1/testapp/client/{ahmed.pk}/", data="{}", content_type="application/json")
        assert resp.status_code == 403

    def test_request_is_api_inside_hooks(self, api, sale):
        seen = {}
        from tests.testapp.admin import SalesAdmin

        original = SalesAdmin.save_model

        def spy(self, request, obj, form, change):
            seen["is_api"], seen["method"] = request.is_api, request.method
            return original(self, request, obj, form, change)

        SalesAdmin.save_model = spy
        try:
            assert api("patch", obj_url(sale), {"slug": "S-1c"})[0] == 200
        finally:
            SalesAdmin.save_model = original
        assert seen == {"is_api": True, "method": "POST"}


@pytest.mark.django_db
class TestPipelineHooks:
    def test_admin_process_changeform_is_used(self, api, ahmed):
        """erp_framework hook point: an admin-defined process_changeform replaces the default."""
        from admin_api.writes import default_process_changeform
        from tests.testapp.admin import ClientAdmin

        calls = []

        def process_changeform(self, request, obj, change):
            calls.append((request.method, change))
            result = default_process_changeform(self, request, obj, change)
            if result.valid and result.obj.name == "Reject":
                raise AssertionError("must not get here for invalid input")
            return result

        def whole_validation_rejects(self, request, obj, change):
            result = default_process_changeform(self, request, obj, change)
            result.form.add_error(None, "Rejected by whole-changeform validation.")
            result.valid = False
            return result

        ClientAdmin.process_changeform = process_changeform
        try:
            status, body = api("patch", f"/api/v1/testapp/client/{ahmed.pk}/", {"name": "Ali"})
            assert status == 200 and calls == [("POST", True)]
            ClientAdmin.process_changeform = whole_validation_rejects
            status, body = api("patch", f"/api/v1/testapp/client/{ahmed.pk}/", {"name": "Zed"})
            assert status == 400 and body["non_field_errors"] == ["Rejected by whole-changeform validation."]
            ahmed.refresh_from_db()
            assert ahmed.name == "Ali"  # invalid result -> rolled back
        finally:
            del ClientAdmin.process_changeform

    def test_reversion_context_is_opened(self, api, sale):
        from tests.testapp.admin import SalesAdmin

        opened = []

        @contextmanager
        def create_revision(self, request):
            opened.append(request.user.username)
            yield

        SalesAdmin.create_revision = create_revision
        try:
            assert api("patch", obj_url(sale), {"slug": "S-1d"})[0] == 200
        finally:
            del SalesAdmin.create_revision
        assert opened == ["root"]
