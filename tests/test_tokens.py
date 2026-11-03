import datetime
import json
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client as HttpClient
from django.utils import timezone

from admin_api.models import ApiToken, hash_secret
from tests.testapp.models import Client


@pytest.fixture
def token(superuser):
    return ApiToken.objects.create_token(superuser, "ci")


def bearer(raw):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


@pytest.mark.django_db
class TestTokenModel:
    def test_only_hash_is_stored(self, token):
        obj, raw = token
        assert raw.startswith(f"aapi_{obj.prefix}_")
        secret = raw.split("_", 2)[2]
        assert obj.key_hash == hash_secret(secret) and secret not in obj.key_hash
        assert ApiToken.objects.get_for_raw(raw) == obj

    @pytest.mark.parametrize("bad", ["", "aapi_", "aapi_nope", "x_y_z", "aapi_deadbeef_wrong"])
    def test_bad_raw(self, token, bad):
        assert ApiToken.objects.get_for_raw(bad) is None

    def test_wrong_secret_same_prefix(self, token):
        obj, _raw = token
        assert ApiToken.objects.get_for_raw(f"aapi_{obj.prefix}_wrong") is None


@pytest.mark.django_db
class TestTokenAuth:
    def test_bearer_token_authenticates(self, token):
        obj, raw = token
        c = HttpClient()
        resp = c.get("/api/v1/", **bearer(raw))
        assert resp.status_code == 200
        obj.refresh_from_db()
        assert obj.last_used_at is not None

    def test_no_csrf_needed_for_token_writes(self, token):
        _, raw = token
        c = HttpClient(enforce_csrf_checks=True)
        resp = c.post(
            "/api/v1/testapp/client/",
            data=json.dumps({"name": "Ahmed"}),
            content_type="application/json",
            **bearer(raw),
        )
        assert resp.status_code == 201, resp.content
        assert Client.objects.filter(name="Ahmed").exists()

    @pytest.mark.parametrize("state", ["revoked", "expired"])
    def test_inactive_token(self, token, state):
        obj, raw = token
        if state == "revoked":
            obj.revoke()
        else:
            obj.expires_at = timezone.now() - datetime.timedelta(seconds=1)
            obj.save()
        resp = HttpClient().get("/api/v1/", **bearer(raw))
        assert resp.status_code == 401 and json.loads(resp.content)["error"] == "invalid_token"

    def test_invalid_token_does_not_fall_back_to_session(self, client, superuser):
        client.force_login(superuser)
        resp = client.get("/api/v1/", **bearer("aapi_nope_nope"))
        assert resp.status_code == 401

    def test_inactive_user(self, token, superuser):
        superuser.is_active = False
        superuser.save()
        assert HttpClient().get("/api/v1/", **bearer(token[1])).status_code == 401

    def test_non_staff_token_is_403(self, plain_user):
        _, raw = ApiToken.objects.create_token(plain_user, "x")
        assert HttpClient().get("/api/v1/", **bearer(raw)).status_code == 403

    def test_permissions_are_the_users(self, staff_view_clients):
        _, raw = ApiToken.objects.create_token(staff_view_clients, "x")
        c = HttpClient()
        assert c.get("/api/v1/testapp/client/", **bearer(raw)).status_code == 200
        assert c.get("/api/v1/testapp/sales/", **bearer(raw)).status_code == 403

    def test_other_auth_scheme_ignored(self, client):
        assert client.get("/api/v1/", HTTP_AUTHORIZATION="Basic abc").status_code == 401


@pytest.mark.django_db
class TestTokenAdmin:
    def test_registered_on_wrapped_site_but_not_exposed(self, client, superuser):
        client.force_login(superuser)
        assert client.get("/admin/admin_api/apitoken/").status_code == 200
        body = json.loads(client.get("/api/v1/").content)
        assert "apitoken" not in {m["model"] for app in body["apps"] for m in app["models"]}
        assert client.get("/api/v1/admin_api/apitoken/").status_code == 404

    def test_add_shows_raw_token_once(self, client, superuser):
        client.force_login(superuser)
        resp = client.post(
            "/admin/admin_api/apitoken/add/", {"user": superuser.pk, "name": "laptop", "kind": "pat"}, follow=True
        )
        assert resp.status_code == 200
        text = next(str(m) for m in resp.context["messages"])
        raw = text.rsplit(" ", 1)[1]
        token = ApiToken.objects.get(name="laptop")
        assert ApiToken.objects.get_for_raw(raw) == token
        assert HttpClient().get("/api/v1/", **bearer(raw)).status_code == 200

    def test_staff_sees_only_own_tokens(self, client, superuser):
        from django.contrib.auth.models import Permission

        staff = get_user_model().objects.create_user("staff2", "s2@x.io", "pw", is_staff=True)
        staff.user_permissions.add(*Permission.objects.filter(codename__endswith="apitoken"))
        ApiToken.objects.create_token(superuser, "root-token")
        ApiToken.objects.create_token(staff, "mine")
        client.force_login(staff)
        resp = client.get("/admin/admin_api/apitoken/")
        assert resp.status_code == 200
        assert [t.name for t in resp.context["cl"].result_list] == ["mine"]

    def test_revoke_action(self, client, superuser, token):
        obj, raw = token
        client.force_login(superuser)
        client.post("/admin/admin_api/apitoken/", {"action": "revoke_tokens", "_selected_action": [obj.pk]})
        obj.refresh_from_db()
        assert obj.revoked_at is not None
        assert HttpClient().get("/api/v1/", **bearer(raw)).status_code == 401


@pytest.mark.django_db
def test_management_command(superuser):
    out = StringIO()
    call_command("create_api_token", "root", "--name", "mcp", "--kind", "mcp", "--expires-days", "30", stdout=out)
    raw = out.getvalue().strip()
    token = ApiToken.objects.get_for_raw(raw)
    assert token.kind == "mcp" and token.expires_at is not None
