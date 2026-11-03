import json
from decimal import Decimal

import pytest
from django.contrib.admin.models import DELETION, LogEntry

from tests.testapp.models import Client, Sales

URL = "/api/v1/testapp/sales/actions/"


@pytest.fixture
def sales(db):
    c = Client.objects.create(name="Ahmed")
    return [
        Sales.objects.create(slug=f"S-{i}", client=c, value=Decimal(v), status="draft")
        for i, v in enumerate(["100", "2000", "3000"], start=1)
    ]


@pytest.fixture
def api(client, superuser):
    client.force_login(superuser)

    def post(name, payload, query=""):
        resp = client.post(f"{URL}{name}/{query}", data=json.dumps(payload), content_type="application/json")
        return resp.status_code, json.loads(resp.content)

    return post


@pytest.mark.django_db
class TestActions:
    def test_run_on_selected_pks(self, api, sales):
        status, body = api("mark_posted", {"pks": [sales[0].pk, sales[1].pk]})
        assert status == 200, body
        assert body == {
            "action": "mark_posted",
            "count": 2,
            "messages": [{"level": "info", "text": "2 posted."}],
            "result": None,
        }
        assert sorted(Sales.objects.filter(status="posted").values_list("slug", flat=True)) == ["S-1", "S-2"]

    def test_all_with_changelist_filters(self, api, sales):
        status, body = api("mark_posted", {"all": True}, query="?band=high")
        assert status == 200 and body["count"] == 2
        assert set(Sales.objects.filter(status="posted").values_list("slug", flat=True)) == {"S-2", "S-3"}

    def test_api_queryset_limits_selection(self, api, sales):
        hidden = Sales.objects.create(slug="HIDDEN", client=sales[0].client)
        status, body = api("mark_posted", {"pks": [hidden.pk]})
        assert status == 400 and body["error"] == "nothing_selected"

    def test_http_response_action_is_rolled_back(self, client, superuser, sales):
        from tests.testapp.admin import SalesAdmin

        twin = SalesAdmin.api_export_csv
        del SalesAdmin.api_export_csv
        try:
            client.force_login(superuser)
            Sales.objects.update(status="posted")
            resp = client.post(f"{URL}export_csv/", data=json.dumps({"all": True}), content_type="application/json")
        finally:
            SalesAdmin.api_export_csv = twin
        assert resp.status_code == 400 and json.loads(resp.content)["error"] == "action_not_supported"
        assert not Sales.objects.filter(status="draft").exists()  # side effect rolled back

    def test_api_twin_returns_data(self, api, sales):
        status, body = api("export_csv", {"pks": [sales[1].pk, sales[0].pk]})
        assert status == 200 and body["result"] == {"rows": [{"slug": "S-1"}, {"slug": "S-2"}]}

    def test_delete_selected_completes(self, api, sales):
        status, body = api("delete_selected", {"pks": [sales[0].pk]})
        assert status == 200, body
        assert not Sales.objects.filter(pk=sales[0].pk).exists()
        assert LogEntry.objects.filter(action_flag=DELETION).count() == 1
        assert "Successfully deleted 1" in body["messages"][0]["text"]

    def test_permission_denied_inside_action(self, api, sales):
        status, body = api("boom", {"pks": [sales[0].pk]})
        assert status == 403 and body["detail"] == "nope"

    def test_unknown_action(self, api, sales):
        assert api("nope", {"pks": [sales[0].pk]})[0] == 404

    def test_nothing_selected(self, api, sales):
        assert api("mark_posted", {})[1]["error"] == "nothing_selected"

    def test_allowed_permissions_filter(self, client, sales):
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Permission

        user = get_user_model().objects.create_user("viewer", "v@x.io", "pw", is_staff=True)
        user.user_permissions.add(Permission.objects.get(codename="view_sales"))
        client.force_login(user)
        resp = client.post(
            f"{URL}mark_posted/", data=json.dumps({"pks": [sales[0].pk]}), content_type="application/json"
        )
        assert resp.status_code == 404  # mark_posted requires change permission

    def test_api_actions_twin(self, api, sales):
        from tests.testapp.admin import SalesAdmin

        SalesAdmin.api_actions = ["mark_posted"]
        try:
            assert api("boom", {"pks": [sales[0].pk]})[0] == 404
            assert api("mark_posted", {"pks": [sales[0].pk]})[0] == 200
        finally:
            del SalesAdmin.api_actions

    def test_schema_lists_actions(self, client, superuser, sales):
        client.force_login(superuser)
        body = json.loads(client.get("/api/v1/testapp/sales/schema/").content)
        actions = {a["name"]: a for a in body["actions"]}
        assert {"delete_selected", "mark_posted", "export_csv", "boom"} <= set(actions)
        assert actions["export_csv"]["api_twin"] is True and actions["mark_posted"]["api_twin"] is False
