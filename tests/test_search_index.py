"""The search index rows: field derivation, post_save, label rows, rebuild and ensure."""

from io import StringIO

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.management import CommandError, call_command
from django.test import override_settings

from admin_api.contrib.search.indexer import Indexer, local_field_name
from admin_api.contrib.search.models import SearchEntry, SearchIndexState
from tests.search_site import PartyAdmin, mcp_site
from tests.testapp.models import Client, Party, Sales, Vendor


def rows(model=None, kind="object"):
    entries = SearchEntry.objects.filter(kind=kind)
    if model is not None:
        entries = entries.filter(content_type=ContentType.objects.get_for_model(model, for_concrete_model=False))
    return sorted(entries.values_list("object_pk", "field", "norm"))


def test_indexed_models_follow_mcp_exposure_and_exclusions(indexer):
    labels = sorted(m._meta.label_lower for m in indexer.admins)
    # Tag is hidden from MCP, the user model is excluded, the API token admin is excluded
    assert labels == ["testapp.client", "testapp.party", "testapp.payment", "testapp.sales"]


def test_index_fields(indexer):
    assert indexer.index_fields(Client) == ("name",)
    assert indexer.index_fields(Sales) == ("slug",)  # client__name is a relation path
    assert indexer.index_fields(Party) == ("name", "code")  # booleans and dates are skipped
    assert indexer.index_fields(Vendor) == ()


@pytest.mark.parametrize(
    "search_field, expected",
    [("^name", "name"), ("=code", "code"), ("name__iexact", "name"), ("client__name", None), ("pk", "id")],
)
def test_local_field_name(search_field, expected):
    model = Party if search_field != "client__name" else Sales
    assert local_field_name(model, search_field) == expected


def test_get_search_index_fields_replaces_the_default(indexer, monkeypatch):
    monkeypatch.setattr(PartyAdmin, "get_search_index_fields", lambda self: ["code"], raising=False)
    assert Indexer(sites=[mcp_site]).index_fields(Party) == ("code",)


def test_post_save_writes_rows_on_commit(indexed):
    Client.objects.create(name="Not yet")
    assert rows(Client) == []  # no commit, no rows
    with indexed():
        alpha = Client.objects.create(name="Ålpha  Trading")
        party = Party.objects.create(name="Acme", code="AC-1")
    assert rows(Client) == [(str(alpha.pk), "name", "alpha trading")]
    assert rows(Party) == [(str(party.pk), "code", "ac-1"), (str(party.pk), "name", "acme")]


def test_save_updates_and_drops_empty_fields(indexed):
    with indexed():
        party = Party.objects.create(name="Acme", code="AC-1")
    before = SearchEntry.objects.get(field="name").updated_at
    with indexed():
        party.code = ""
        party.save()
    assert rows(Party) == [(str(party.pk), "name", "acme")]
    assert SearchEntry.objects.get(field="name").updated_at == before  # norm unchanged
    with indexed():
        party.name = "Acme Corp"
        party.save()
    assert SearchEntry.objects.get(field="name").updated_at > before


def test_raw_saves_are_skipped(indexer, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks() as callbacks:
        from django.db.models.signals import post_save

        post_save.send(Client, instance=Client(pk=99, name="Raw"), created=True, raw=True)
    assert callbacks == []


def test_unindexed_models_are_skipped(indexed):
    from tests.testapp.models import Tag

    with indexed():
        Tag.objects.create(name="hidden")
    assert SearchEntry.objects.filter(kind="object").count() == 0


def test_multi_table_child_rows_belong_to_the_parent_admin(indexed):
    with indexed():
        vendor = Vendor.objects.create(name="Nile Supplies", code="V-7", city="Cairo")
    # Vendor has no admin: its rows use Party's content type and Party's fields
    assert rows(Party) == [(str(vendor.pk), "code", "v-7"), (str(vendor.pk), "name", "nile supplies")]
    assert rows(Vendor) == []


def test_polymorphic_leaf_content_type(indexer):
    party = Party(pk=5, name="Leaf")
    vendor_ct = ContentType.objects.get_for_model(Vendor)
    party.polymorphic_ctype_id = vendor_ct.pk  # what django-polymorphic stores on the base row
    content_type, admin_model = indexer.row_target(party)
    assert (content_type, admin_model) == (vendor_ct, Party)


@override_settings(LANGUAGES=[("en", "English"), ("de", "German")])
def test_model_rows(indexer):
    indexer.refresh_model_rows()
    party_rows = SearchEntry.objects.filter(
        kind="model", content_type=ContentType.objects.get_for_model(Party)
    ).values_list("field", "language", "norm")
    # one row per folded text: "party" is both the verbose name and the model name; German repeats English
    assert sorted(party_rows) == [
        ("alias.0", "en", "suppliers"),
        ("verbose_name", "en", "party"),
        ("verbose_name_plural", "en", "parties"),
    ]
    indexer.refresh_model_rows()
    assert SearchEntry.objects.filter(kind="model").count() == len(indexer.model_entries())


def test_rebuild_batches_prunes_and_is_rerunnable(indexer):
    parties = [Party.objects.create(name=f"Party {i}") for i in range(5)]
    out = StringIO()
    call_command("rebuild_search_index", "--batch-size", "2", stdout=out)
    assert "processed 2/5 testapp.party" in out.getvalue()
    assert "processed 4/5 testapp.party" in out.getvalue()
    assert "processed 5/5 testapp.party" in out.getvalue()
    assert len(rows(Party)) == 5
    Party.objects.filter(pk=parties[0].pk).delete()  # no signal for deletes
    call_command("rebuild_search_index", "--model", "testapp.Party", stdout=StringIO())
    assert [pk for pk, _f, _n in rows(Party)] == sorted(str(p.pk) for p in parties[1:])
    call_command("rebuild_search_index", stdout=StringIO())
    assert len(rows(Party)) == 4  # a second run keeps unchanged rows


def test_rebuild_prunes_rows_of_fields_no_longer_indexed(indexer, monkeypatch):
    Party.objects.create(name="Acme", code="AC-1")
    call_command("rebuild_search_index", stdout=StringIO())
    monkeypatch.setattr(PartyAdmin, "get_search_index_fields", lambda self: ["name"], raising=False)
    call_command("rebuild_search_index", "--model", "testapp.party", stdout=StringIO())
    assert [f for _pk, f, _n in rows(Party)] == ["name"]


def test_rebuild_options(indexer):
    Party.objects.create(name="Acme")
    call_command("rebuild_search_index", "--models-only", stdout=StringIO())
    assert rows(Party) == [] and rows(Party, kind="model")
    with pytest.raises(CommandError, match="Not indexed: testapp.tag"):
        call_command("rebuild_search_index", "--model", "testapp.tag")
    with pytest.raises(CommandError, match="batch-size"):
        call_command("rebuild_search_index", "--batch-size", "0")


def test_ensure_rebuilds_only_what_changed(indexer, monkeypatch):
    Party.objects.create(name="Acme", code="AC-1")
    out = StringIO()
    call_command("search_index", "ensure", stdout=out)
    assert "rebuilt: testapp.client, testapp.sales, testapp.party" in out.getvalue()
    assert SearchIndexState.objects.count() == 4  # Payment has no index fields but is tracked
    out = StringIO()
    call_command("search_index", "ensure", stdout=out)
    assert "rebuilt: nothing" in out.getvalue()
    monkeypatch.setattr(PartyAdmin, "get_search_index_fields", lambda self: ["name"], raising=False)
    out = StringIO()
    call_command("search_index", "ensure", stdout=out)
    assert "rebuilt: testapp.party" in out.getvalue()


def test_ensure_drops_models_no_longer_indexed(indexer):
    Party.objects.create(name="Acme")
    call_command("search_index", "ensure", stdout=StringIO())
    narrowed = Indexer(sites=[type(mcp_site)(mcp_site.api_site, name="narrow", excluded_models=["testapp.party"])])
    from admin_api.contrib.search import indexer as indexer_module

    indexer_module.set_indexer(narrowed)
    out = StringIO()
    call_command("search_index", "ensure", stdout=out)
    assert "dropped testapp.party" in out.getvalue()
    assert rows(Party) == []
    assert not SearchIndexState.objects.filter(label="testapp.party").exists()


def test_ensure_refuses_an_unsafe_ctype(indexer, monkeypatch):
    from admin_api.contrib.search.management.commands import search_index

    monkeypatch.setattr(search_index, "database_ctype", lambda: "C")
    with pytest.raises(CommandError, match="LC_CTYPE is 'C'"):
        call_command("search_index", "ensure")


def test_post_migrate_refreshes_model_rows(indexer):
    from admin_api.contrib.search.indexer import on_post_migrate

    SearchEntry.objects.all().delete()
    on_post_migrate()
    assert rows(Party, kind="model")
