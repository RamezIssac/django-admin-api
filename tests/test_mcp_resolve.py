"""resolve_entities: names to ids and models, through the admin's own scoping."""

from decimal import Decimal

import pytest
from django import forms
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.models import Group, Permission
from django.utils import translation

from admin_api import AdminApiSite
from admin_api.mcp import AdminMcpSite
from admin_api.mcp.resolve import CONFIDENCE, Hit, LiveEntityBackend, list_status, match_text, normalize
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
site.register(Group, GroupAdmin)
api_site = AdminApiSite(site, name="resolve_api")


class ClientSearchAdmin(admin.ModelAdmin):
    search_fields = ["^name", "notes__iexact"]


class PaymentBySalesValueForm(forms.ModelForm):
    sales = forms.ModelChoiceField(queryset=Sales.objects.all(), to_field_name="value")

    class Meta:
        model = Payment
        fields = ["client", "amount"]


class PaymentBySalesValueAdmin(admin.ModelAdmin):
    form = PaymentBySalesValueForm


class ClientNotSearchedByApi(testadmin.ClientAdmin):
    api_search_fields = []


class SalesByApiOrdering(testadmin.SalesAdmin):
    api_ordering = ["-slug"]


custom_site = admin.AdminSite(name="resolve_custom")
custom_site.register(Client, ClientSearchAdmin)
custom_site.register(Sales, testadmin.SalesAdmin)
custom_site.register(Payment, PaymentBySalesValueAdmin)
custom_api_site = AdminApiSite(custom_site, name="resolve_custom_api")

twins_site = admin.AdminSite(name="resolve_twins")
twins_site.register(Client, ClientNotSearchedByApi)
twins_site.register(Sales, SalesByApiOrdering)
twins_api_site = AdminApiSite(twins_site, name="resolve_twins_api")


@pytest.fixture
def mcp():
    return AdminMcpSite(api_site)


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


def test_searched_models_only_when_none_or_verbose(resolve, data):
    found = resolve("Alpha")["objects"]
    assert found["status"] == "ambiguous" and "searched_models" not in found
    assert resolve("Alpha", verbose=True)["objects"]["searched_models"] == ["testapp.client", "testapp.sales"]
    assert resolve("egypt")["objects"]["searched_models"] == ["testapp.client", "testapp.sales"]
    out = resolve("Alpha", verbose="yes")
    assert out["error"] == "invalid_arguments"


def test_scoped_get_queryset_hides_a_row(resolve, data):
    assert Client.objects.filter(name="Bob").exists()
    assert resolve("bob")["objects"]["status"] == "none"
    assert resolve("HIDDEN")["objects"]["status"] == "none"  # SalesAdmin.get_api_queryset


def test_live_search_fields_twin(resolve, data):
    # SalesAdmin.search_fields has client__name; its api_search_fields twin does not
    out = resolve({"text": "Ahmed Ali", "models": ["testapp.sales"]})
    assert out["objects"]["status"] == "none"


def test_no_view_permission_hides_a_model(resolve, data, staff_view_clients):
    out = resolve("alpha", user=staff_view_clients, verbose=True)
    assert out["objects"]["searched_models"] == ["testapp.client"]
    assert ids(out["objects"]) == [("testapp.client", data["clients"]["Alpha"].pk)]
    assert out["objects"]["status"] == "unique"
    assert resolve("sales", user=staff_view_clients)["models"]["status"] == "none"


def test_token_scope(resolve, data, superuser):
    out = resolve("alpha", scopes=["testapp.sales:view"], verbose=True)
    assert out["objects"]["searched_models"] == ["testapp.sales"]
    assert ids(out["objects"]) == [("testapp.sales", data["sales"]["alpha"].pk)]
    assert resolve({"text": "payment", "kind": "models"}, scopes=["testapp.sales:view"])["models"]["status"] == "none"


def test_excluded_models(resolve, data):
    # auth is excluded by default although its admin is exposed
    assert resolve({"text": "root", "kind": "objects"})["objects"]["status"] == "none"
    assert resolve("users")["models"]["status"] == "none"
    open_site = AdminMcpSite(api_site, excluded_models=())
    assert resolve({"text": "root", "kind": "objects"}, site=open_site)["objects"]["candidates"][0]["label"] == "root"
    no_sales = AdminMcpSite(api_site, excluded_models=("auth", "testapp.sales"))
    assert resolve("alpha", site=no_sales, verbose=True)["objects"]["searched_models"] == ["testapp.client"]


def test_hidden_model_answers_like_an_unknown_one(resolve, data):
    Tag.objects.create(name="alpha")
    for label in ("testapp.tag", "testapp.nope", "testapp.secret"):
        assert resolve({"text": "alpha", "models": [label]})["objects"] == {
            "status": "none",
            "more": False,
            "candidates": [],
            "searched_models": [],
        }
    assert resolve({"text": "tag", "kind": "models"})["models"]["status"] == "none"


def test_for_field_with_limit_choices_to(resolve, data):
    field = {"model": "testapp.payment", "field": "client"}
    out = resolve({"text": "a", "for_field": field}, limit=20, verbose=True)
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


TERMS_LIST = "'terms' must be a list of at least one term."
MODELS_LIST = "'models' must be a list of model labels."
FOR_FIELD_SHAPE = "'for_field' must be an object with 'model' and 'field'."


@pytest.mark.parametrize(
    "args, detail",
    [
        ({"terms": []}, TERMS_LIST),
        ({"terms": "ahmed"}, TERMS_LIST),
        ({"terms": ["ahmed"]}, "Each entry of 'terms' must be an object with 'text'."),
        ({"terms": [{"text": " "}]}, "Each term needs a non-empty 'text'."),
        ({"terms": [{"text": "a", "kind": "rows"}]}, "'kind' must be one of any, objects, models."),
        (
            {"terms": [{"text": "a", "kind": "models", "models": ["testapp.client"]}]},
            "'models' and 'for_field' narrow an object search; they do not go with kind='models'.",
        ),
        ({"terms": [{"text": "a", "models": "testapp.client"}]}, MODELS_LIST),
        ({"terms": [{"text": "a", "kind": "models"}], "models": "testapp.client"}, MODELS_LIST),
        ({"terms": [{"text": "a"}], "limit": "5"}, "'limit' must be an integer."),
        ({"terms": [{"text": "a"}] * 21}, "At most 20 terms per call."),
        ({"terms": [{"text": "a", "for_field": "client"}]}, FOR_FIELD_SHAPE),
        ({"terms": [{"text": "a", "for_field": {"model": "testapp.payment"}}]}, FOR_FIELD_SHAPE),
    ],
)
def test_bad_arguments(rf, superuser, mcp, db, args, detail):
    request = rf.post("/mcp/")
    request.user = superuser
    result = mcp.call_tool(request, "resolve_entities", args)
    assert result.is_error and result.data["error"] == "invalid_arguments"
    assert result.data["detail"] == detail


def test_to_many_search_field_returns_each_object_once(resolve, data):
    class ClientBySales(admin.ModelAdmin):
        search_fields = ["name", "sales__slug"]

    by_sales = admin.AdminSite(name="resolve_by_sales")
    by_sales.register(Client, ClientBySales)
    Sales.objects.create(slug="S-10", client=data["clients"]["Ahmed Ali"])
    out = resolve("S-1", site=AdminMcpSite(AdminApiSite(by_sales, name="resolve_by_sales_api")))
    assert [(c["label"], c["match"], c["matched_field"]) for c in out["objects"]["candidates"]] == [
        ("Ahmed Ali", "exact_code", "sales__slug")
    ]
    assert out["objects"]["status"] == "unique"


def test_top_level_models_is_the_default_for_each_term(resolve, data, mcp):
    first, own, field, models_only = resolve(
        "alpha",
        {"text": "alpha", "models": ["testapp.sales"]},
        {"text": "s-1", "for_field": {"model": "testapp.payment", "field": "inlines.lines.sales"}},
        {"text": "sales", "kind": "models"},
        models=["testapp.client"],
        verbose=True,
    )
    assert first["objects"]["searched_models"] == ["testapp.client"]
    assert first["objects"]["status"] == "unique"
    assert "models" not in first
    assert own["objects"]["searched_models"] == ["testapp.sales"]
    assert field["objects"]["searched_models"] == ["testapp.sales"]
    assert models_only["models"]["status"] == "unique"
    schema = mcp.get_tool("resolve_entities").input_schema
    assert schema["properties"]["models"]["type"] == "array"
    assert resolve("alpha", models="testapp.client")["error"] == "invalid_arguments"


@pytest.mark.parametrize(
    "term, value, code, expected",
    [
        ("ahmed ali", "  Ahmed \t ALI ", False, "exact"),
        ("s-1", "S-1", True, "exact_code"),
        ("ahm", "Ahmed Ali", False, "prefix"),
        ("ali", "Ahmed Ali", False, "word"),
        ("sal", "Ahmed Saleh", False, "word_prefix"),
        ("med", "Ahmed Ali", False, "contains"),
        ("", "Ahmed", False, None),
        ("ahmed", "", False, None),
        ("bob", "Ahmed", False, None),
    ],
)
def test_match_text(term, value, code, expected):
    assert match_text(term, value, code=code) == expected


def test_normalize():
    assert normalize("  Ahmed \t ALI ") == "ahmed ali"


def _hit(match, confidence=None):
    return Hit(None, match=match, confidence=CONFIDENCE[match] if confidence is None else confidence)


@pytest.mark.parametrize(
    "hits, status",
    [
        ([], "none"),
        ([_hit("exact"), _hit("prefix")], "unique"),
        ([_hit("exact"), _hit("exact_code")], "ambiguous"),
        ([_hit("prefix")], "unique"),
        ([_hit("search", 0.7)], "unique"),
        ([_hit("prefix"), _hit("word")], "ambiguous"),
        ([_hit("contains")], "ambiguous"),
    ],
)
def test_list_status(hits, status):
    assert list_status(hits) == status


def test_term_and_limit_bounds(resolve, data):
    assert len(resolve(*["ahmed"] * 20)) == 20
    assert len(resolve("ahmed", limit=0)["objects"]["candidates"]) == 1
    out = resolve("ahmed", limit=2)
    assert len(out["objects"]["candidates"]) == 2 and out["objects"]["more"] is False
    assert resolve({"text": "sales", "kind": "models"}, limit=1)["models"]["more"] is False


def test_for_field_error_messages(resolve, data):
    def detail(field, model="testapp.payment", **kwargs):
        return resolve({"text": "x", "for_field": {"model": model, "field": field}}, **kwargs)["detail"]

    assert detail("amount") == "'amount' is not a relation field of testapp.payment's form."
    assert detail("inlines.nope.sales") == "No inline 'nope' on testapp.payment. Inlines: lines."
    assert detail("inlines.nope.x", model="testapp.client") == "No inline 'nope' on testapp.client. Inlines: none."
    assert detail("client", model="testapp.nope") == "This kind of record is not available here."
    no_clients = AdminMcpSite(api_site, excluded_models=("auth", "testapp.client"))
    assert detail("client", site=no_clients) == "This kind of record is not available here."


def test_for_field_reads_the_add_form(resolve, data):
    # a user who may add payments but not change them still sees the add form's fields
    user = get_user_model().objects.create_user("adder", "a@x.io", "pw", is_staff=True)
    user.user_permissions.add(*Permission.objects.filter(codename__in=["view_payment", "add_payment", "view_client"]))
    out = resolve({"text": "alpha", "for_field": {"model": "testapp.payment", "field": "client"}}, user=user)
    assert ids(out["objects"]) == [("testapp.client", data["clients"]["Alpha"].pk)]


def test_for_field_to_field_name_gives_the_id(resolve, data):
    Sales.objects.filter(slug="S-1").update(value=Decimal("12.50"))
    field = {"model": "testapp.payment", "field": "sales"}
    out = resolve({"text": "S-1", "for_field": field}, site=AdminMcpSite(custom_api_site))
    assert ids(out["objects"]) == [("testapp.sales", "12.50")]


def test_search_field_prefixes_and_lookups(resolve, data):
    Client.objects.create(name="Zed", notes="zed")
    Client.objects.create(name="Yan", notes="VIP")
    mcp = AdminMcpSite(custom_api_site)
    zed = resolve({"text": "zed", "models": ["testapp.client"]}, site=mcp)["objects"]["candidates"]
    assert [(c["label"], c["match"], c["matched_field"]) for c in zed] == [("Zed", "exact", "name")]
    vip = resolve({"text": "vip", "models": ["testapp.client"]}, site=mcp)["objects"]["candidates"]
    assert [(c["label"], c["match"], c["matched_field"]) for c in vip] == [("Yan", "exact", "notes")]


def test_scan_limit_caps_and_keeps_exact_matches(resolve, data, monkeypatch):
    # the newest rows fill the scan; the oldest row still comes back through its exact match
    monkeypatch.setattr(LiveEntityBackend, "scan_limit", 2)
    for name in ("Zara", "Zarab", "Zarac", "Zarabb"):
        Client.objects.create(name=name)
    mcp = AdminMcpSite(custom_api_site)
    out = resolve({"text": "zara", "models": ["testapp.client"]}, site=mcp, limit=20)["objects"]
    assert [(c["label"], c["match"]) for c in out["candidates"]] == [
        ("Zara", "exact"),
        ("Zarabb", "prefix"),
        ("Zarac", "prefix"),
    ]
    assert out["status"] == "unique" and out["more"] is True
    out = resolve({"text": "zarab", "models": ["testapp.client"]}, site=mcp, limit=20)["objects"]
    assert len(out["candidates"]) == 2 and out["more"] is False


def test_api_twins_drive_search_fields_and_ordering(resolve, data):
    Sales.objects.create(slug="S-3", client=data["clients"]["Alpha"])
    Sales.objects.create(slug="S-2", client=data["clients"]["Alpha"])
    out = resolve("s-", site=AdminMcpSite(twins_api_site), verbose=True)["objects"]
    assert out["searched_models"] == ["testapp.sales"]
    assert [c["label"] for c in out["candidates"]] == ["S-3", "S-2", "S-1"]


class _RecordingBackend(LiveEntityBackend):
    """Puts a hit outside the scoped queryset first and records the requests it gets."""

    def __init__(self, site):
        super().__init__(site)
        self.requests = []

    def search_objects(self, request, term, targets):
        self.requests.append(request)
        hits, capped = super().search_objects(request, term, targets)
        sales = next(t for t in targets if t.label == "testapp.sales")
        return [Hit(sales, Sales.objects.get(slug="HIDDEN").pk), *hits], capped

    def search_models(self, request, term, targets):
        self.requests.append(request)
        return super().search_models(request, term, targets)


def test_entity_backend_seam(rf, superuser, data):
    mcp = AdminMcpSite(api_site)
    mcp.entity_backend_class = _RecordingBackend
    assert mcp.get_entity_backend().site is mcp
    backends = []
    mcp.get_entity_backend = lambda: backends.append(_RecordingBackend(mcp)) or backends[-1]
    request = rf.post("/mcp/")
    request.user = superuser
    out = mcp.call_tool(request, "resolve_entities", {"terms": [{"text": "s-1"}]}).data["results"][0]
    assert ids(out["objects"]) == [("testapp.sales", data["sales"]["S-1"].pk)]
    assert len(backends[0].requests) == 2 and all(r is request for r in backends[0].requests)


def test_models_ranked_best_first(resolve, data):
    out = resolve({"text": "s", "kind": "models"})["models"]["candidates"]
    assert [(c["model"], c["match"]) for c in out] == [
        ("testapp.sales", "prefix"),
        ("testapp.client", "contains"),
        ("testapp.payment", "contains"),
    ]


def test_models_match_translated_names(resolve, data, settings):
    open_site = AdminMcpSite(api_site, excluded_models=())
    settings.LANGUAGES = [("en", "English"), ("de", "German")]
    out = resolve({"text": "gruppe", "kind": "models"}, site=open_site)["models"]
    assert out["candidates"] == [
        {"model": "auth.group", "label": "Gruppe", "language": "de", "confidence": 1.0, "match": "exact"}
    ]


def test_models_use_the_active_language(resolve, data):
    open_site = AdminMcpSite(api_site, excluded_models=())
    with translation.override("de"):
        out = resolve({"text": "benutzer", "kind": "models"}, site=open_site)["models"]
    assert [(c["model"], c["language"]) for c in out["candidates"]] == [("auth.user", "de")]
