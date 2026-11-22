"""resolve_entities over the search index (the db engine), with the admin's own scoping."""

import datetime
from io import StringIO

import pytest
from django.contrib import admin
from django.core.management import call_command

from admin_api import AdminApiSite
from admin_api.contrib.search.backend import SearchEntityBackend
from admin_api.contrib.search.indexer import Indexer, set_indexer
from admin_api.mcp import AdminMcpSite
from admin_api.mcp.resolve import LiveEntityBackend
from tests.search_site import PartyAdmin, api_site, mcp_site, picks
from tests.testapp.models import Client, Holiday, Party, Sales, Ticket, Vendor, Voucher


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
    out = search_resolve("alpha", user=staff_view_clients)
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
    assert picks(out["models"], "model", "label", "match", "language") == [
        ("testapp.party", "suppliers", "exact", "en")
    ]
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


def rebuild_parties(*names_and_codes):
    for name, code in names_and_codes:
        Party.objects.create(name=name, code=code)
    call_command("rebuild_search_index", "--model", "testapp.party", verbosity=0, stdout=StringIO())


@pytest.mark.parametrize(
    "older, best",
    [
        ([("Zedd a", ""), ("Zedd b", "")], "Zed"),  # exact over prefix
        ([("Ab Zed", ""), ("Cd Zed", "")], "Zedd"),  # prefix over word
        ([("Ab Zedx", ""), ("Cd Zedy", "")], "Ab Zed"),  # word over word_prefix
        ([("Abzed", ""), ("Cdzed", "")], "Ab Zedx"),  # word_prefix over contains
        ([("P1", "abzed"), ("P2", "abzed")], "Zedd"),  # a code field only ranks exact_code on an exact match
        ([("P1", "zed"), ("P2", "zed")], "Zed"),  # exact (1.0) over exact_code (0.95)
    ],
)
def test_engine_rank_decides_the_top_candidates(search_resolve, indexer, older, best):
    # the best row is the newest pk: on a tie in rank the older rows would win the two slots
    rebuild_parties(*older, (best, ""))
    out = search_resolve({"text": "zed", "models": ["testapp.party"]}, limit=1)
    assert out["objects"]["candidates"][0]["label"] == best


def test_rank_ties_go_to_the_latest_change(search_resolve, indexed):
    rebuild_parties(("Zedd 1", ""), ("Zedd 2", ""), ("Zedd 3", ""))
    with indexed():
        latest = Party.objects.get(name="Zedd 3")
        latest.name = "Zedd 3x"
        latest.save()
    out = search_resolve({"text": "zedd", "models": ["testapp.party"]}, limit=1)
    assert out["objects"]["candidates"][0]["label"] == "Zedd 3x"


def test_search_index_code_fields(search_resolve, indexer, monkeypatch):
    rebuild_parties(("Acme", ""))
    assert picks(search_resolve({"text": "acme", "kind": "objects"})["objects"], "match") == [("exact",)]
    monkeypatch.setattr(PartyAdmin, "search_index_code_fields", ("name",), raising=False)
    assert picks(search_resolve({"text": "acme", "kind": "objects"})["objects"], "match") == [("exact_code",)]


def test_a_term_that_folds_to_nothing_finds_nothing(search_resolve, data):
    out = search_resolve({"text": "\u0640", "kind": "objects"})  # a lone tatweel
    assert out["objects"]["status"] == "none" and out["objects"]["more"] is False


def test_models_ranked_best_first(search_resolve, indexer, monkeypatch, settings):
    settings.LANGUAGES = [("en", "English")]
    monkeypatch.setattr(PartyAdmin, "search_aliases", {"en": ["sales team"]})
    call_command("rebuild_search_index", "--models-only", verbosity=0, stdout=StringIO())
    out = search_resolve({"text": "sales", "kind": "models"})
    assert picks(out["models"], "model", "match") == [("testapp.sales", "exact"), ("testapp.party", "prefix")]


class TitleAdmin(admin.ModelAdmin):
    search_fields = ["title"]

    def get_queryset(self, request):
        return super().get_queryset(request).exclude(title__startswith="Hidden")


class VendorAdmin(admin.ModelAdmin):
    search_fields = ["name"]

    def get_queryset(self, request):
        return super().get_queryset(request).exclude(name__startswith="Hidden")


pk_types = admin.AdminSite(name="search_pk_types")
for _model in (Ticket, Voucher, Holiday):
    pk_types.register(_model, TitleAdmin)
pk_types.register(Vendor, VendorAdmin)  # its pk is the parent link to Party
pk_types_mcp = AdminMcpSite(AdminApiSite(pk_types, name="search_pk_types_api"), name="search_pk_types_mcp")


@pytest.fixture
def pk_types_indexed(indexer):
    set_indexer(Indexer(sites=[pk_types_mcp]))
    for prefix in ("", "Hidden "):
        Ticket.objects.create(title=f"{prefix}Zed ticket")
        Voucher.objects.create(code=f"{prefix}V", title=f"{prefix}Zed voucher")
        Holiday.objects.create(day=datetime.date(2025, 1, 2 if prefix else 1), title=f"{prefix}Zed holiday")
        Vendor.objects.create(name=f"{prefix}Zed vendor")
    call_command("rebuild_search_index", verbosity=0, stdout=StringIO())


@pytest.mark.parametrize(
    "model, label",
    [
        ("testapp.ticket", "Zed ticket"),
        ("testapp.voucher", "Zed voucher"),
        ("testapp.holiday", "Zed holiday"),
        ("testapp.vendor", "Zed vendor"),
    ],
)
def test_scoping_matches_each_primary_key_type(search_resolve, pk_types_indexed, model, label):
    out = search_resolve({"text": "zed", "models": [model]}, site=pk_types_mcp)
    assert picks(out["objects"], "label") == [(label,)]
