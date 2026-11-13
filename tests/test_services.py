"""The transport-neutral service layer, called directly (as the MCP tools will)."""

from decimal import Decimal

import pytest

from admin_api import AdminApiSite, services
from admin_api.errors import ApiError, NotFound, PermissionDeniedError
from admin_api.writes import ValidationFailed
from tests.testapp.admin import site
from tests.testapp.models import Client, Sales
from tests.urls import api_site


@pytest.fixture
def req(rf, superuser):
    def make(user=superuser, query=""):
        r = rf.get(f"/?{query}")
        r.user = user
        r.is_api = True
        return r

    return make


def test_lookup_by_label(req):
    admin = services.get_model_admin_by_label(api_site, req(), "testapp.Sales")
    assert admin.model is Sales


@pytest.mark.parametrize("label", ["", "sales", "testapp.", ".sales"])
def test_lookup_bad_label(req, label):
    with pytest.raises(ApiError) as exc:
        services.get_model_admin_by_label(api_site, req(), label)
    assert exc.value.code == "bad_model"


def test_lookup_unknown_and_forbidden(req, staff_view_clients):
    with pytest.raises(NotFound):
        services.get_model_admin_by_label(api_site, req(), "testapp.nope")
    with pytest.raises(PermissionDeniedError):
        services.get_model_admin_by_label(api_site, req(staff_view_clients), "testapp.sales")


def test_registry_without_mounted_urls(req):
    unmounted = AdminApiSite(admin_site=site, name="never_mounted", register_token_admin=False)
    data = services.registry(unmounted, req())
    models = {m["model"]: m for app in data["apps"] for m in app["models"]}
    assert "sales" in models
    assert models["sales"]["url"] is None


def test_changelist_reads_request_get(req):
    ahmed = Client.objects.create(name="Ahmed")
    Sales.objects.create(slug="S-1", client=ahmed, value=Decimal(5), status="draft")
    Sales.objects.create(slug="S-2", client=ahmed, value=Decimal(5), status="posted")
    admin = services.get_model_admin(api_site, req(), "testapp", "sales")
    data = services.changelist(api_site, admin, req(query="status__exact=posted&_shape=columnar"))
    assert data["count"] == 1
    assert data["columns"][0] == "pk"


def test_create_checks_permission_before_loading_payload(req, staff_view_clients):
    admin = api_site.get_model_admin("testapp", "client")

    def payload():
        raise AssertionError("payload must not be parsed before the permission check")

    with pytest.raises(PermissionDeniedError):
        services.create(admin, req(staff_view_clients), payload)


def test_create_and_validation_errors(req):
    admin = services.get_model_admin(api_site, req(), "testapp", "client")
    created = services.create(admin, req(), {"name": "Bassem"})
    assert created["name"] == "Bassem"
    with pytest.raises(ValidationFailed) as exc:
        services.create(admin, req(), {"name": ""})
    assert "name" in exc.value.extra["fields"]


def test_action_payload_must_be_object(req):
    admin = services.get_model_admin(api_site, req(), "testapp", "sales")
    with pytest.raises(ApiError) as exc:
        services.run_action(admin, req(), "delete_selected", ["not", "an", "object"])
    assert exc.value.code == "invalid_json"
