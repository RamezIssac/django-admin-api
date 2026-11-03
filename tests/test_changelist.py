import datetime
import json
from decimal import Decimal

import pytest
from django.contrib import admin
from django.db import connection
from django.test.utils import CaptureQueriesContext

import admin_api
from admin_api.changelist import get_columns
from tests.testapp.models import Client, Sales

URL = "/api/v1/testapp/sales/"


@pytest.fixture
def data(db):
    ahmed = Client.objects.create(name="Ahmed")
    bassem = Client.objects.create(name="Bassem")
    Sales.objects.create(slug="S-1", client=ahmed, value=Decimal(500), date=datetime.date(2025, 1, 5))
    Sales.objects.create(
        slug="S-2", client=bassem, value=Decimal(1500), status="posted", date=datetime.date(2025, 2, 1)
    )
    Sales.objects.create(slug="S-3", client=ahmed, value=Decimal(2000), status="posted", date=datetime.date(2024, 6, 1))
    Sales.objects.create(slug="HIDDEN", client=ahmed, value=Decimal(1))
    return {"ahmed": ahmed, "bassem": bassem}


@pytest.fixture
def api(client, superuser):
    client.force_login(superuser)

    def get(url=URL, **params):
        resp = client.get(url, params)
        return resp.status_code, json.loads(resp.content)

    return get


@pytest.mark.django_db
class TestList:
    def test_rows_and_encoding(self, api, data):
        status, body = api()
        assert status == 200
        assert body["count"] == 3  # HIDDEN excluded by get_api_queryset (plain admin, no mixin)
        assert body["per_page"] == 2 and body["pages"] == 2 and body["page"] == 1
        assert body["next"].endswith("?p=2") and body["previous"] is None
        first = body["results"][0]
        assert first == {
            "pk": first["pk"],
            "slug": "S-1",
            "date": "2025-01-05",
            "client": {"pk": data["ahmed"].pk, "str": "Ahmed"},
            "status": "draft",
            "value": "500.00",
            "doubled": "1000.00",
        }
        # "badge" (HTML callable, not marked) is not exposed
        assert "badge" not in first

    def test_page_2(self, api, data):
        status, body = api(p=2)
        assert status == 200 and [r["slug"] for r in body["results"]] == ["S-3"]
        assert body["next"] is None and body["previous"].endswith("p=1")

    def test_per_page_param(self, api, data):
        _, body = api(_per_page=10)
        assert body["pages"] == 1 and len(body["results"]) == 3

    @pytest.mark.parametrize("value", ["0", "9999", "x"])
    def test_per_page_bounds(self, api, data, value):
        status, body = api(_per_page=value)
        assert status == 400 and body["error"] == "bad_lookup"

    def test_columnar_shape(self, api, data):
        _, body = api(_shape="columnar", _per_page=10)
        assert body["columns"] == ["pk", "slug", "date", "client", "status", "value", "doubled"]
        assert [row[1] for row in body["rows"]] == ["S-1", "S-2", "S-3"]
        assert "results" not in body

    def test_bad_shape(self, api, data):
        assert api(_shape="xml")[0] == 400

    def test_field_filter(self, api, data):
        _, body = api(status__exact="posted")
        assert {r["slug"] for r in body["results"]} == {"S-2", "S-3"}

    def test_fk_filter(self, api, data):
        _, body = api(client__id__exact=data["bassem"].pk)
        assert [r["slug"] for r in body["results"]] == ["S-2"]

    def test_simple_list_filter(self, api, data):
        _, body = api(band="high")
        assert {r["slug"] for r in body["results"]} == {"S-2", "S-3"}

    def test_date_hierarchy(self, api, data):
        _, body = api(date__year=2025)
        assert {r["slug"] for r in body["results"]} == {"S-1", "S-2"}

    def test_search_uses_api_twin(self, api, data):
        # search_fields includes client__name, api_search_fields does not
        assert api(q="S-2")[1]["count"] == 1
        assert api(q="Bassem")[1]["count"] == 0

    def test_ordering_by_index(self, api, data):
        _, body = api(o="-4", _per_page=10)  # index 4 = value, descending
        assert [r["slug"] for r in body["results"]] == ["S-3", "S-2", "S-1"]

    def test_ordering_by_marked_callable(self, api, data):
        _, body = api(o="5", _per_page=10)  # doubled -> admin_order_field "value"
        assert [r["slug"] for r in body["results"]] == ["S-1", "S-2", "S-3"]

    def test_bad_lookup(self, api, data):
        status, body = api(nope__exact=1)
        assert status == 400 and body["error"] == "bad_lookup"

    def test_page_out_of_range(self, api, data):
        assert api(p=99)[0] == 400

    def test_unknown_model_404(self, api):
        assert api("/api/v1/testapp/nope/")[0] == 404

    def test_hidden_model_404(self, api):
        assert api("/api/v1/testapp/secret/")[0] == 404

    def test_permission_denied(self, client, staff_view_clients, data):
        client.force_login(staff_view_clients)  # can view clients, not sales
        assert client.get(URL).status_code == 403
        assert client.get("/api/v1/testapp/client/").status_code == 200

    def test_explicit_twin_exposes_unmarked_callable(self, api, data):
        _, body = api("/api/v1/testapp/client/")
        assert body["results"] == [{"pk": data["ahmed"].pk, "name": "Ahmed", "notes_length": 0}]


@pytest.mark.django_db
class TestQueryBudget:
    def _count(self, api, n):
        ahmed = Client.objects.create(name=f"Ahmed{n}")
        Sales.objects.bulk_create([Sales(slug=f"Q-{n}-{i}", client=ahmed) for i in range(n)])
        with CaptureQueriesContext(connection) as ctx:
            status, body = api(_per_page=50, q=f"Q-{n}-")
        assert status == 200 and body["count"] == n
        return len(ctx.captured_queries)

    def test_fk_column_does_not_cause_n_plus_1(self, api):
        assert self._count(api, 1) == self._count(api, 20)


@pytest.mark.django_db
class TestSchema:
    def test_schema(self, api, data):
        status, body = api(URL + "schema/")
        assert status == 200
        assert body["model"]["model"] == "sales" and body["model"]["pk"] == "id"
        cols = {c["name"]: c for c in body["columns"]}
        assert list(cols) == ["slug", "date", "client", "status", "value", "doubled"]
        assert cols["client"]["type"] == "related" and cols["date"]["type"] == "date"
        assert cols["status"]["choices"] == [
            {"value": "draft", "display": "Draft"},
            {"value": "posted", "display": "Posted"},
        ]
        assert cols["doubled"]["kind"] == "callable" and cols["doubled"]["sortable"] is True
        assert cols["value"]["order_index"] == 4
        filters = {f["title"]: f for f in body["filters"]}
        assert set(filters) == {"status", "client", "value band"}
        band = {c["display"]: c["params"] for c in filters["value band"]["choices"]}
        assert band["High (>= 1000)"] == {"band": "high"}
        client_choices = {c["display"]: c["params"] for c in filters["client"]["choices"]}
        assert client_choices["Bassem"] == {"client__id__exact": str(data["bassem"].pk)}
        assert body["search"]["enabled"] is True
        assert body["date_hierarchy"] == "date"
        assert body["pagination"]["per_page"] == 2

    def test_schema_does_not_fetch_rows(self, api, data):
        with CaptureQueriesContext(connection) as ctx:
            api(URL + "schema/")
        sales_selects = [q for q in ctx.captured_queries if 'FROM "testapp_sales"' in q["sql"]]
        assert sales_selects == []


class TestColumns:
    def _admin(self, **body):
        return type("A", (admin.ModelAdmin,), body)(Sales, admin.AdminSite(name="cols"))

    def test_unmarked_callable_hidden_marked_shown(self, api_request):
        def plain(obj):
            return 1

        marked = admin_api.display(lambda obj: 2)
        ma = self._admin(list_display=["slug", plain, marked, "__str__"])
        kinds = [(c.key, c.kind) for c in get_columns(ma, api_request)]
        assert kinds == [("slug", "field"), ("<lambda>", "callable"), ("str", "str")]

    def test_model_method_needs_marking(self, api_request):
        ma = self._admin(list_display=["slug", "get_status_display"])
        assert [c.key for c in get_columns(ma, api_request)] == ["slug"]
        ma = self._admin(list_display=["slug"], api_list_display=["slug", "get_status_display"])
        assert [c.key for c in get_columns(ma, api_request)] == ["slug", "get_status_display"]
