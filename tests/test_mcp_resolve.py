"""resolve_entities: names to ids and models, through the admin's own scoping."""

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin

from admin_api import AdminApiSite
from admin_api.mcp import AdminMcpSite
from admin_api.mcp.resolve import LiveEntityBackend
from admin_api.models import ApiToken
from tests.testapp import admin as testadmin
from tests.testapp.models import Client, Payment, PaymentLine, Sales, Tag


class PaymentLineInline(admin.TabularInline):
    model = PaymentLine


class PaymentAdmin(admin.ModelAdmin):
    inlines = [PaymentLineInline]


site = admin.AdminSite(name="resolve_admin")
site.register(Client, testadmin.ClientAdmin)
site.register(Sales, testadmin.SalesAdmin)
site.register(Tag, testadmin.TagAdmin)
site.register(Payment, PaymentAdmin)
site.register(get_user_model(), UserAdmin)
api_site = AdminApiSite(site, name="resolve_api")


class LiveMcpSite(AdminMcpSite):
    entity_backend_class = LiveEntityBackend  # the search backend: tests/test_search_resolve.py


@pytest.fixture
def mcp():
    return LiveMcpSite(api_site)


@pytest.fixture
def resolve(rf, superuser, mcp):
    def do(*terms, user=superuser, site=mcp, scopes=None, **args):
        request = rf.post("/mcp/")
        request.user = user
        if scopes is not None:
            request.api_token = ApiToken.objects.create_token(user, "t", scopes=scopes)[0]
        terms = [t if isinstance(t, dict) else {"text": t} for t in terms]
        result = site.call_tool(request, "resolve_entities", {"terms": terms, **args})
        if result.is_error:
            return result.data
        return result.data["results"][0] if len(terms) == 1 else result.data["results"]

    return do


@pytest.fixture
def data(db):
    clients = {
        name: Client.objects.create(name=name, notes=notes)
        for name, notes in [("Ahmed Ali", ""), ("Ahmed Saleh", ""), ("Alpha", ""), ("Amira", "on hold"), ("Bob", "")]
    }
    sales = {slug: Sales.objects.create(slug=slug, client=clients["Ahmed Ali"]) for slug in ("S-1", "alpha", "HIDDEN")}
    return {"clients": clients, "sales": sales}


def ids(listing):
    return [(c["model"], c["id"]) for c in listing["candidates"]]


def test_listed_as_a_read_tool(mcp):
    assert "resolve_entities" in [t.name for t in AdminMcpSite(api_site, read_only=True).get_tools()]
    assert mcp.get_tool("resolve_entities").annotations["readOnlyHint"] is True
    assert "resolve_entities" in mcp.instructions


def test_ambiguous_prefix(resolve, data):
    out = resolve("ahmed")
    assert out["term"] == "ahmed"
    assert out["objects"]["status"] == "ambiguous"
    assert [(c["label"], c["match"], c["confidence"], c["matched_field"]) for c in out["objects"]["candidates"]] == [
        ("Ahmed Saleh", "prefix", 0.85, "name"),
        ("Ahmed Ali", "prefix", 0.85, "name"),
    ]
    assert out["models"] == {"status": "none", "more": False, "candidates": []}


def test_unique_exact(resolve, data):
    out = resolve("ahmed  ALI")
    assert out["objects"]["status"] == "unique"
    assert out["objects"]["candidates"] == [
        {
            "model": "testapp.client",
            "id": data["clients"]["Ahmed Ali"].pk,
            "label": "Ahmed Ali",
            "confidence": 1.0,
            "match": "exact",
            "matched_field": "name",
        }
    ]


def test_cross_model_exact_is_ambiguous(resolve, data):
    out = resolve("Alpha")
    assert out["objects"]["status"] == "ambiguous"
    assert [(c["model"], c["match"]) for c in out["objects"]["candidates"]] == [
        ("testapp.client", "exact"),
        ("testapp.sales", "exact_code"),
    ]


def test_single_weak_candidate_is_ambiguous(resolve, data):
    out = resolve("lph")
    assert [(c["label"], c["match"], c["confidence"]) for c in out["objects"]["candidates"]] == [
        ("Alpha", "contains", 0.6),
        ("alpha", "contains", 0.6),
    ]
    assert resolve({"text": "lph", "models": ["testapp.client"]})["objects"]["status"] == "ambiguous"


def test_none(resolve, data):
    out = resolve("egypt")
    assert out["objects"] == {
        "status": "none",
        "more": False,
        "candidates": [],
        "searched_models": ["testapp.client", "testapp.sales"],
    }
    assert out["models"]["status"] == "none"


def test_scoped_get_queryset_hides_a_row(resolve, data):
    assert Client.objects.filter(name="Bob").exists()
    assert resolve("bob")["objects"]["status"] == "none"
    assert resolve("HIDDEN")["objects"]["status"] == "none"  # SalesAdmin.get_api_queryset


def test_live_search_fields_twin(resolve, data):
    # SalesAdmin.search_fields has client__name; its api_search_fields twin does not
    out = resolve({"text": "Ahmed Ali", "models": ["testapp.sales"]})
    assert out["objects"]["status"] == "none"


def test_no_view_permission_hides_a_model(resolve, data, staff_view_clients):
    out = resolve("alpha", user=staff_view_clients)
    assert out["objects"]["searched_models"] == ["testapp.client"]
    assert ids(out["objects"]) == [("testapp.client", data["clients"]["Alpha"].pk)]
    assert out["objects"]["status"] == "unique"
    assert resolve("sales", user=staff_view_clients)["models"]["status"] == "none"


def test_token_scope(resolve, data, superuser):
    out = resolve("alpha", scopes=["testapp.sales:view"])
    assert out["objects"]["searched_models"] == ["testapp.sales"]
    assert ids(out["objects"]) == [("testapp.sales", data["sales"]["alpha"].pk)]


def test_excluded_models(resolve, data):
    # auth is excluded by default although its admin is exposed
    assert resolve({"text": "root", "kind": "objects"})["objects"]["status"] == "none"
    assert resolve("users")["models"]["status"] == "none"
    open_site = LiveMcpSite(api_site, excluded_models=())
    assert resolve({"text": "root", "kind": "objects"}, site=open_site)["objects"]["candidates"][0]["label"] == "root"
    no_sales = LiveMcpSite(api_site, excluded_models=("auth", "testapp.sales"))
    assert resolve("alpha", site=no_sales)["objects"]["searched_models"] == ["testapp.client"]


def test_hidden_model_answers_like_an_unknown_one(resolve, data):
    Tag.objects.create(name="alpha")
    for label in ("testapp.tag", "testapp.nope", "testapp.secret"):
        assert resolve({"text": "alpha", "models": [label]})["objects"] == {
            "status": "none",
            "more": False,
            "candidates": [],
            "searched_models": [],
        }


def test_for_field_with_limit_choices_to(resolve, data):
    field = {"model": "testapp.payment", "field": "client"}
    out = resolve({"text": "a", "for_field": field}, limit=20)
    assert "models" not in out
    assert out["objects"]["searched_models"] == ["testapp.client"]
    # Amira has notes (limit_choices_to), Bob is outside ClientAdmin's queryset
    assert sorted(c["label"] for c in out["objects"]["candidates"]) == ["Ahmed Ali", "Ahmed Saleh", "Alpha"]
    assert resolve({"text": "amira", "for_field": field})["objects"]["status"] == "none"
    assert resolve({"text": "amira", "models": ["testapp.client"]})["objects"]["status"] == "unique"


def test_for_field_inline(resolve, data):
    field = {"model": "testapp.payment", "field": "inlines.lines.sales"}
    out = resolve({"text": "s-1", "for_field": field})
    assert out["objects"]["status"] == "unique"
    assert out["objects"]["candidates"][0]["match"] == "exact_code"
    assert resolve({"text": "HIDDEN", "for_field": field})["objects"]["status"] == "none"


def test_for_field_errors(resolve, data, staff_view_clients):
    out = resolve({"text": "x", "for_field": {"model": "testapp.payment", "field": "amount"}})
    assert out["error"] == "invalid_arguments"
    out = resolve({"text": "x", "for_field": {"model": "testapp.payment", "field": "inlines.nope.sales"}})
    assert out["error"] == "invalid_arguments"
    hidden = resolve(
        {"text": "x", "for_field": {"model": "testapp.payment", "field": "client"}}, user=staff_view_clients
    )
    unknown = resolve({"text": "x", "for_field": {"model": "testapp.nope", "field": "client"}})
    assert hidden["error"] == unknown["error"] == "not_found"


def test_models(resolve, data):
    out = resolve("sales")
    assert out["models"]["status"] == "unique"
    assert out["models"]["candidates"][0] == {
        "model": "testapp.sales",
        "label": "sales",
        "language": "en-us",
        "confidence": 1.0,
        "match": "exact",
    }
    clients = resolve({"text": "Clients", "kind": "models"})
    assert "objects" not in clients
    assert [(c["model"], c["match"]) for c in clients["models"]["candidates"]] == [("testapp.client", "exact")]


def test_models_in_every_language(resolve, data, settings):
    settings.LANGUAGES = [("en", "English"), ("de", "German")]
    out = resolve({"text": "payments", "kind": "models"})
    assert out["models"]["candidates"][0]["language"] == "en"


def test_limit_and_more(resolve, data):
    out = resolve("a", limit=2)
    assert len(out["objects"]["candidates"]) == 2
    assert out["objects"]["more"] is True
    assert resolve("a", limit=500)["objects"]["more"] is False


def test_several_terms(resolve, data):
    first, second = resolve("ahmed ali", {"text": "s-1", "models": ["testapp.sales"]})
    assert first["objects"]["status"] == second["objects"]["status"] == "unique"
    assert "models" not in second


@pytest.mark.parametrize(
    "args",
    [
        {"terms": []},
        {"terms": "ahmed"},
        {"terms": [{"text": " "}]},
        {"terms": [{"text": "a", "kind": "rows"}]},
        {"terms": [{"text": "a", "kind": "models", "models": ["testapp.client"]}]},
        {"terms": [{"text": "a", "models": "testapp.client"}]},
        {"terms": [{"text": "a"}], "limit": "5"},
        {"terms": [{"text": "a"}] * 21},
    ],
)
def test_bad_arguments(rf, superuser, mcp, db, args):
    request = rf.post("/mcp/")
    request.user = superuser
    result = mcp.call_tool(request, "resolve_entities", args)
    assert result.is_error and result.data["error"] == "invalid_arguments"


def test_to_many_search_field_returns_each_object_once(resolve, data):
    class ClientBySales(admin.ModelAdmin):
        search_fields = ["name", "sales__slug"]

    by_sales = admin.AdminSite(name="resolve_by_sales")
    by_sales.register(Client, ClientBySales)
    Sales.objects.create(slug="S-10", client=data["clients"]["Ahmed Ali"])
    out = resolve("S-1", site=LiveMcpSite(AdminApiSite(by_sales, name="resolve_by_sales_api")))
    assert [(c["label"], c["match"], c["matched_field"]) for c in out["objects"]["candidates"]] == [
        ("Ahmed Ali", "exact_code", "sales__slug")
    ]
    assert out["objects"]["status"] == "unique"
