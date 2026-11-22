"""resolve_entities over the search index (the db engine), with the admin's own scoping."""

from io import StringIO

import pytest
from django.contrib import admin
from django.core.management import call_command

from admin_api import AdminApiSite
from admin_api.contrib.search.backend import SearchEntityBackend
from admin_api.mcp import AdminMcpSite
from admin_api.mcp.resolve import LiveEntityBackend
from tests.search_site import api_site, mcp_site, picks
from tests.testapp.models import Client, Party, Sales


@pytest.fixture
def data(indexer):
    clients = {
        name: Client.objects.create(name=name, notes=notes)
        for name, notes in [("Ahmed Ali", ""), ("Ahmed Saleh", ""), ("Alpha", ""), ("Amira", "on hold"), ("Bob", "")]
    }
    sales = {slug: Sales.objects.create(slug=slug, client=clients["Ahmed Ali"]) for slug in ("S-1", "alpha", "HIDDEN")}
    parties = {
        name: Party.objects.create(name=name, code=code) for name, code in [("أحمد علي", ""), ("مدرسة النور", "")]
    }
    call_command("rebuild_search_index", verbosity=0, stdout=StringIO())
    return {"clients": clients, "sales": sales, "parties": parties}


def test_installed_app_selects_the_search_backend():
    assert isinstance(mcp_site.get_entity_backend(), SearchEntityBackend)

    class Live(AdminMcpSite):
        entity_backend_class = LiveEntityBackend

    assert isinstance(Live(api_site, name="live").get_entity_backend(), LiveEntityBackend)


def test_ambiguous_prefix(search_resolve, data):
    out = search_resolve("ahmed")
    assert out["objects"]["status"] == "ambiguous"
    # equal scores and one rebuild batch: ties fall back to the content type and pk
    assert picks(out["objects"], "label", "match", "confidence", "matched_field") == [
        ("Ahmed Ali", "prefix", 0.85, "name"),
        ("Ahmed Saleh", "prefix", 0.85, "name"),
    ]


def test_unique_exact(search_resolve, data):
    out = search_resolve("ahmed  ALI")
    assert out["objects"]["status"] == "unique"
    assert out["objects"]["candidates"][0] == {
        "model": "testapp.client",
        "id": data["clients"]["Ahmed Ali"].pk,
        "label": "Ahmed Ali",
        "confidence": 1.0,
        "match": "exact",
        "matched_field": "name",
    }


def test_cross_model_exact_is_ambiguous(search_resolve, data):
    out = search_resolve("Alpha")
    assert out["objects"]["status"] == "ambiguous"
    assert picks(out["objects"], "model", "match")[:2] == [("testapp.client", "exact"), ("testapp.sales", "exact_code")]


@pytest.mark.parametrize(
    "term, match, confidence, status",
    [
        ("saleh", "word", 0.8, "unique"),
        ("sal", "word_prefix", 0.75, "unique"),
        ("hmed sal", "contains", 0.6, "ambiguous"),
    ],
)
def test_match_types(search_resolve, data, term, match, confidence, status):
    out = search_resolve({"text": term, "models": ["testapp.client"]})
    assert picks(out["objects"], "label", "match", "confidence")[0] == ("Ahmed Saleh", match, confidence)
    assert out["objects"]["status"] == status


def test_short_terms_match_exact_and_prefix_only(search_resolve, data):
    out = search_resolve({"text": "al", "models": ["testapp.client", "testapp.sales"]})
    # "Ahmed Ali" holds the word "ali", but two letters only match a prefix
    assert sorted(picks(out["objects"], "model", "match")) == [
        ("testapp.client", "prefix"),
        ("testapp.sales", "prefix"),
    ]


def test_arabic_folding(search_resolve, data):
    out = search_resolve({"text": "احمد", "models": ["testapp.party"]})
    assert picks(out["objects"], "label", "match") == [("أحمد علي", "prefix")]
    out = search_resolve({"text": "النور", "models": ["testapp.party"]})
    assert picks(out["objects"], "label", "match") == [("مدرسة النور", "word")]
    assert search_resolve({"text": "مدرسه", "models": ["testapp.party"]})["objects"]["status"] == "unique"


def test_scoped_get_queryset_hides_a_row(search_resolve, data):
    assert search_resolve("bob")["objects"]["status"] == "none"  # ClientAdmin.get_api_queryset
    assert search_resolve("HIDDEN")["objects"]["status"] == "none"  # SalesAdmin.get_api_queryset


def test_deleted_objects_drop_out(search_resolve, data):
    data["clients"]["Ahmed Saleh"].delete()  # no signal: the row stays until the next rebuild
    out = search_resolve({"text": "ahmed", "models": ["testapp.client"]})
    assert picks(out["objects"], "label") == [("Ahmed Ali",)]
    assert out["objects"]["status"] == "unique"


def test_live_search_fields_filter_rows(search_resolve, data):
    class NameOnly(admin.ModelAdmin):
        search_fields = ["name"]

    narrow = admin.AdminSite(name="search_narrow")
    narrow.register(Party, NameOnly)
    site = AdminMcpSite(AdminApiSite(narrow, name="search_narrow_api"), name="search_narrow_mcp")
    Party.objects.create(name="Nile", code="ZX-9")
    call_command("rebuild_search_index", "--model", "testapp.party", verbosity=0, stdout=StringIO())
    assert search_resolve({"text": "zx-9"})["objects"]["status"] == "unique"
    assert search_resolve({"text": "zx-9"}, site=site)["objects"]["status"] == "none"


def test_no_view_permission_hides_a_model(search_resolve, data, staff_view_clients):
    out = search_resolve("alpha", user=staff_view_clients, verbose=True)
    assert out["objects"]["searched_models"] == ["testapp.client"]
    assert picks(out["objects"], "model", "id") == [("testapp.client", data["clients"]["Alpha"].pk)]
    assert search_resolve("sales", user=staff_view_clients)["models"]["status"] == "none"


def test_token_scope(search_resolve, data):
    out = search_resolve("alpha", scopes=["testapp.sales:view"])
    assert picks(out["objects"], "model", "id") == [("testapp.sales", data["sales"]["alpha"].pk)]


def test_excluded_models(search_resolve, data, superuser):
    assert search_resolve({"text": "root", "kind": "objects"})["objects"]["status"] == "none"
    no_sales = AdminMcpSite(api_site, name="search_no_sales", excluded_models=("auth", "testapp.sales"))
    out = search_resolve("alpha", site=no_sales)
    assert picks(out["objects"], "model") == [("testapp.client",)]


def test_for_field_with_limit_choices_to(search_resolve, data):
    field = {"model": "testapp.payment", "field": "client"}
    out = search_resolve({"text": "ahmed", "for_field": field})
    assert sorted(c["label"] for c in out["objects"]["candidates"]) == ["Ahmed Ali", "Ahmed Saleh"]
    assert search_resolve({"text": "amira", "for_field": field})["objects"]["status"] == "none"  # has notes
    assert search_resolve({"text": "amira", "models": ["testapp.client"]})["objects"]["status"] == "unique"


def test_models(search_resolve, data, settings):
    settings.LANGUAGES = [("en", "English"), ("de", "German")]
    call_command("rebuild_search_index", "--models-only", verbosity=0, stdout=StringIO())
    out = search_resolve({"text": "Parties", "kind": "models"})
    assert out["models"] == {
        "status": "unique",
        "more": False,
        "candidates": [
            {"model": "testapp.party", "label": "parties", "language": "en", "confidence": 1.0, "match": "exact"}
        ],
    }
    out = search_resolve({"text": "suppliers", "kind": "models"})  # PartyAdmin.search_aliases
    assert picks(out["models"], "model", "label", "match") == [("testapp.party", "suppliers", "exact")]
    out = search_resolve({"text": "sale", "kind": "models"})
    assert picks(out["models"], "model", "match") == [("testapp.sales", "prefix")]
    assert search_resolve({"text": "users", "kind": "models"})["models"]["status"] == "none"  # excluded


def test_limit_and_more(search_resolve, data):
    for i in range(6):
        Client.objects.create(name=f"Anna {i}")
    call_command("rebuild_search_index", "--model", "testapp.client", verbosity=0, stdout=StringIO())
    out = search_resolve({"text": "anna", "models": ["testapp.client"]}, limit=5)
    assert len(out["objects"]["candidates"]) == 5
    assert out["objects"]["more"] is True
    assert search_resolve({"text": "anna", "models": ["testapp.client"]}, limit=20)["objects"]["more"] is False


def test_saved_objects_are_found_without_a_rebuild(search_resolve, indexed, indexer):
    with indexed():
        client = Client.objects.create(name="Apex Trading")
    out = search_resolve({"text": "apex", "models": ["testapp.client"]})
    assert picks(out["objects"], "id", "match") == [(client.pk, "prefix")]
