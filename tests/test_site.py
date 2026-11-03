import json

import pytest
from django.core import checks
from django.test import override_settings

from tests.testapp.models import Client


def get_json(client, url="/api/v1/"):
    resp = client.get(url)
    return resp.status_code, json.loads(resp.content)


@pytest.mark.django_db
class TestRegistry:
    def test_anonymous_is_401(self, client):
        status, body = get_json(client)
        assert status == 401 and body["error"] == "not_authenticated"

    def test_non_staff_is_403(self, client, plain_user):
        client.force_login(plain_user)
        status, body = get_json(client)
        assert status == 403 and body["error"] == "permission_denied"

    def test_superuser_sees_exposed_models_only(self, client, superuser):
        client.force_login(superuser)
        status, body = get_json(client)
        assert status == 200 and body["version"] == "v1"
        models = {m["model"] for app in body["apps"] for m in app["models"]}
        assert models == {"client", "sales", "tag"}  # Secret has api_enabled = False
        entry = next(m for app in body["apps"] for m in app["models"] if m["model"] == "client")
        assert entry["url"] == "/api/v1/testapp/client/"
        assert entry["perms"] == {"add": True, "change": True, "delete": True, "view": True}

    def test_permissions_mirror_the_admin(self, client, staff_view_clients):
        client.force_login(staff_view_clients)
        status, body = get_json(client)
        assert status == 200
        models = [m for app in body["apps"] for m in app["models"]]
        assert [m["model"] for m in models] == ["client"]
        assert models[0]["perms"] == {"add": False, "change": False, "delete": False, "view": True}

    def test_method_not_allowed(self, client, superuser):
        client.force_login(superuser)
        resp = client.post("/api/v1/")
        assert resp.status_code == 405


@pytest.mark.django_db
class TestSessionCsrf:
    """Session auth enforces CSRF on unsafe methods (token auth, later, will not)."""

    def _post_view(self):
        from django.http import JsonResponse

        from tests.urls import api_site

        return api_site.api_view(lambda request: JsonResponse({"ok": True}), methods=("POST",))

    def _request(self, rf, user, token=None):
        from django.middleware.csrf import _get_new_csrf_string, _mask_cipher_secret

        secret = _get_new_csrf_string()
        headers = {"HTTP_X_CSRFTOKEN": _mask_cipher_secret(secret)} if token else {}
        req = rf.post("/x", **headers)
        req.COOKIES["csrftoken"] = secret
        req.user = user
        return req

    def test_missing_token_is_rejected(self, rf, superuser):
        resp = self._post_view()(self._request(rf, superuser))
        assert resp.status_code == 403
        assert json.loads(resp.content)["error"] == "csrf_failed"

    def test_valid_token_passes(self, rf, superuser):
        resp = self._post_view()(self._request(rf, superuser, token=True))
        assert resp.status_code == 200


@pytest.mark.django_db
class TestIsApiFlag:
    def test_middleware_marks_html_requests(self, client, superuser):
        client.force_login(superuser)
        seen = {}
        from tests.testapp.admin import ClientAdmin

        original = ClientAdmin.get_list_display

        def spy(self, request):
            seen["is_api"] = request.is_api
            return original(self, request)

        ClientAdmin.get_list_display = spy
        try:
            Client.objects.create(name="Ahmed")
            assert client.get("/admin/testapp/client/").status_code == 200
        finally:
            ClientAdmin.get_list_display = original
        assert seen["is_api"] is False


class TestChecks:
    @override_settings(MIDDLEWARE=[])
    def test_e001_missing_middleware(self):
        ids = [e.id for e in checks.run_checks()]
        assert "admin_api.E001" in ids
