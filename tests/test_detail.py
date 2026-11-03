import json
from decimal import Decimal

import pytest
from django.contrib.admin.models import DELETION, LogEntry
from django.db import connection
from django.test.utils import CaptureQueriesContext

from tests.testapp.models import Client, Sales, SalesItem, Tag


@pytest.fixture
def sale(db):
    ahmed = Client.objects.create(name="Ahmed")
    s = Sales.objects.create(slug="S-1", client=ahmed, value=Decimal(500))
    s.tags.add(Tag.objects.create(name="vip"))
    SalesItem.objects.create(sales=s, product="Cheese", quantity=2, price=Decimal(100))
    SalesItem.objects.create(sales=s, product="Milk", quantity=3, price=Decimal(100))
    return s


@pytest.fixture
def api(client, superuser):
    client.force_login(superuser)

    def call(method, url, **kw):
        resp = getattr(client, method)(url, **kw)
        return resp.status_code, json.loads(resp.content) if resp.content else None

    return call


def url(obj, suffix=""):
    return f"/api/v1/testapp/{obj._meta.model_name}/{obj.pk}/{suffix}"


@pytest.mark.django_db
class TestDetail:
    def test_detail_payload(self, api, sale):
        status, body = api("get", url(sale))
        assert status == 200
        items = body.pop("inlines")["items"]
        assert body == {
            "pk": sale.pk,
            "str": "S-1",
            "slug": "S-1",
            "date": "2025-01-01",
            "client": {"pk": sale.client.pk, "str": "Ahmed"},
            "status": "draft",
            "value": "500.00",
            "tags": [{"pk": sale.tags.get().pk, "str": "vip"}],
            "doubled": "1000.00",  # marked readonly callable; "badge" (unmarked HTML) is hidden
        }
        assert [(i["product"], i["quantity"], i["price"]) for i in items] == [
            ("Cheese", 2, "100.00"),
            ("Milk", 3, "100.00"),
        ]
        assert "sales" not in items[0]  # FK to parent is implied

    def test_api_detail_fields_twin(self, api, sale):
        from tests.testapp.admin import SalesAdmin

        SalesAdmin.api_detail_fields = ["slug", "badge"]  # explicit: unmarked callable allowed
        try:
            _, body = api("get", url(sale))
        finally:
            del SalesAdmin.api_detail_fields
        assert body["slug"] == "S-1" and body["badge"] == "<b>S-1</b>"
        assert "value" not in body

    def test_api_queryset_applies(self, api, db):
        hidden = Sales.objects.create(slug="HIDDEN", client=Client.objects.create(name="X"))
        assert api("get", url(hidden))[0] == 404

    def test_not_found(self, api, db):
        assert api("get", "/api/v1/testapp/sales/999/")[0] == 404
        assert api("get", "/api/v1/testapp/sales/abc/")[0] == 404

    def test_view_permission(self, client, staff_view_clients, sale):
        client.force_login(staff_view_clients)
        assert client.get(url(sale)).status_code == 403
        assert client.get(url(sale.client)).status_code == 200

    def test_inline_rows_do_not_cause_n_plus_1(self, api, sale):
        def count():
            with CaptureQueriesContext(connection) as ctx:
                assert api("get", url(sale))[0] == 200
            return len(ctx.captured_queries)

        before = count()
        SalesItem.objects.bulk_create([SalesItem(sales=sale, product=f"P{i}") for i in range(10)])
        assert count() == before


@pytest.mark.django_db
class TestDelete:
    def test_preview(self, api, sale):
        status, body = api("get", url(sale, "delete-preview/"))
        assert status == 200
        assert body["object"] == {"pk": sale.pk, "str": "S-1"}
        assert body["to_delete"][0] == "Sales: S-1"
        assert "Sales item: 2 x Cheese" in body["to_delete"][1]
        assert body["model_count"]["sales items"] == 2 and body["model_count"]["sales"] == 1
        assert body["can_delete"] is True

    def test_delete(self, api, sale):
        pk = sale.pk
        status, body = api("delete", url(sale))
        assert status == 200 and body == {"deleted": {"pk": pk, "str": "S-1"}}
        assert not Sales.objects.filter(pk=pk).exists()
        assert not SalesItem.objects.filter(sales_id=pk).exists()
        assert LogEntry.objects.filter(action_flag=DELETION, object_id=str(pk)).exists()

    def test_protected(self, api, sale):
        status, body = api("delete", url(sale.client))
        assert status == 409 and body["error"] == "protected"
        assert body["protected"] == ["Sales: S-1"]
        assert Client.objects.filter(pk=sale.client.pk).exists()

    def test_delete_requires_permission(self, client, staff_view_clients, sale):
        client.force_login(staff_view_clients)
        assert client.delete(url(sale.client)).status_code == 403
        assert client.get(url(sale.client, "delete-preview/")).status_code == 403

    def test_delete_requires_csrf_for_session(self, superuser, sale):
        from django.test import Client as HttpClient

        c = HttpClient(enforce_csrf_checks=True)
        c.force_login(superuser)
        resp = c.delete(url(sale))
        assert resp.status_code == 403 and json.loads(resp.content)["error"] == "csrf_failed"


@pytest.mark.django_db
@pytest.mark.parametrize("tz", ["UTC", "America/Chicago"])
def test_datetimes_use_current_timezone_everywhere(settings, tz):
    import datetime

    from django.utils import timezone

    from admin_api.encoders import encode_value

    settings.TIME_ZONE = tz
    utc = datetime.datetime(2025, 4, 1, 9, 0, tzinfo=datetime.timezone.utc)
    with timezone.override(tz):
        local = timezone.localtime(utc)
        assert encode_value(utc) == encode_value(local)
