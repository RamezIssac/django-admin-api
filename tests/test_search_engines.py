"""The engine seam: ADMIN_API_SEARCH settings and the post-filter path of engines without scope_pushdown."""

from io import StringIO

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command

from admin_api.contrib.search.conf import get_engine, search_settings
from admin_api.contrib.search.engines.base import Hit, SearchEngine
from admin_api.contrib.search.engines.db import DatabaseEngine
from tests.search_site import picks
from tests.testapp.models import Client


class MemoryEngine(SearchEngine):
    """Rows in a dict, substring matching, insertion order, no scoping of its own."""

    scope_pushdown = False

    def __init__(self, name="", options=None):
        super().__init__(name, options)
        self.rows, self.queries = {}, []

    def upsert(self, entries):
        for e in entries:
            self.rows[(e.kind, e.content_type.pk, e.object_pk, e.field, e.language)] = e

    def delete_rows(self, content_type, object_pk, fields):
        for key in [k for k in self.rows if k[1] == content_type.pk and k[2] == object_pk and k[3] in fields]:
            del self.rows[key]

    def replace_model_rows(self, rows):
        self.rows = {k: v for k, v in self.rows.items() if k[0] != "model"}
        self.upsert(rows)

    def prune(self, content_types, older_than):
        return 0

    def search(self, query):
        self.queries.append(query)
        allowed = {(ct.pk, f) for s in query.scopes for ct in s.content_types for f in s.fields or [None]}
        found, keys = [], []
        for e in self.rows.values():
            field = e.field if query.kind == "object" else None
            if e.kind != query.kind or (e.content_type.pk, field) not in allowed:
                continue
            if query.text in e.norm or e.norm.startswith(query.text[:2]):
                found.append(e)
                if (e.content_type.pk, e.object_pk) not in keys:
                    keys.append((e.content_type.pk, e.object_pk))
        page = set(keys[query.offset : query.offset + query.limit])
        return [
            Hit(e.content_type, e.object_pk, e.field, e.norm, engine_score=0.5)
            for e in found
            if (e.content_type.pk, e.object_pk) in page
        ]


@pytest.fixture
def memory(settings):
    settings.ADMIN_API_SEARCH = {
        "DEFAULT_ENGINE": "memory",
        "ENGINES": {"memory": {"BACKEND": "tests.test_search_engines.MemoryEngine"}},
    }
    yield get_engine()
    get_engine.cache_clear()


def test_settings_merge_with_the_defaults(settings):
    assert search_settings()["DEFAULT_ENGINE"] == "db"
    assert isinstance(get_engine(), DatabaseEngine)
    settings.ADMIN_API_SEARCH = {"ENGINES": {"memory": {"BACKEND": "tests.test_search_engines.MemoryEngine"}}}
    assert set(search_settings()["ENGINES"]) == {"db", "memory"}
    settings.ADMIN_API_SEARCH = {"DEFAULT_ENGINE": "nope"}
    with pytest.raises(ImproperlyConfigured, match="no engine 'nope'"):
        get_engine()
    get_engine.cache_clear()


def test_post_filter_drops_rows_the_admin_hides(memory, search_resolve, indexer):
    Client.objects.create(name="Bzed hidden")  # ClientAdmin's API queryset: names starting with "A"
    visible = Client.objects.create(name="Azed")
    call_command("rebuild_search_index", stdout=StringIO())
    out = search_resolve({"text": "zed", "models": ["testapp.client"]})
    assert picks(out["objects"], "id", "match") == [(visible.pk, "contains")]
    assert out["objects"]["status"] == "ambiguous"  # one candidate under 0.7


def test_post_filter_pages_up_to_three_times(memory, search_resolve, indexer):
    for i in range(25):
        Client.objects.create(name=f"Bzed {i}")
    visible = Client.objects.create(name="Azed")
    call_command("rebuild_search_index", stdout=StringIO())
    memory.queries.clear()
    out = search_resolve({"text": "zed", "models": ["testapp.client"]}, limit=1)
    # limit 1 asks for 2 candidates, 5 x 2 hits per page: the visible row is on page 3
    assert [(q.offset, q.limit) for q in memory.queries if q.kind == "object"] == [(0, 10), (10, 10), (20, 10)]
    assert picks(out["objects"], "id") == [(visible.pk,)]
    for i in range(25, 35):
        Client.objects.create(name=f"Bzed {i}")
    Client.objects.filter(pk=visible.pk).delete()
    Client.objects.create(name="Azed again")
    call_command("rebuild_search_index", stdout=StringIO())
    assert search_resolve({"text": "zed", "models": ["testapp.client"]}, limit=1)["objects"]["status"] == "none"


def test_fuzzy_hits_score_from_the_engine(memory, search_resolve, indexer):
    Client.objects.create(name="Amadeus")
    call_command("rebuild_search_index", stdout=StringIO())
    out = search_resolve({"text": "amx", "models": ["testapp.client"]})
    assert picks(out["objects"], "label", "match", "confidence") == [("Amadeus", "fuzzy", 0.3)]
