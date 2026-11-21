"""The db engine's Postgres paths: pg_trgm, the GIN index, the trigram pass and the ctype check.
Run with ``--ds tests.settings_postgres`` (nox session ``tests_postgres``)."""

from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection

from admin_api.contrib.search.checks import check_engine
from admin_api.contrib.search.conf import get_engine
from admin_api.contrib.search.engines import db as db_engine
from tests.search_site import picks
from tests.testapp.models import Party

pytestmark = pytest.mark.skipif(connection.vendor != "postgresql", reason="Postgres only (tests.settings_postgres)")


@pytest.fixture
def parties(indexer):
    for name in ("Debugging", "John Doe", "John Max", "Server Maintenance", "الاسلام"):
        Party.objects.create(name=name)
    call_command("rebuild_search_index", stdout=StringIO())


def query(sql, params=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def test_migration_installs_pg_trgm_and_the_gin_index(db):
    assert query("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'") == [(1,)]
    (definition,) = query("SELECT indexdef FROM pg_indexes WHERE indexname = 'admin_api_search_entry_norm_trgm'")[0]
    assert "gin (norm gin_trgm_ops)" in definition
    assert get_engine().fuzzy is True


def test_like_pass_can_use_the_gin_index(parties):
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL enable_seqscan = off")
        cursor.execute("EXPLAIN SELECT id FROM admin_api_search_entry WHERE norm LIKE %s", ["%john%"])
        plan = "\n".join(row[0] for row in cursor.fetchall())
    assert "admin_api_search_entry_norm_trgm" in plan


def test_trigram_pass_finds_a_typo(search_resolve, parties):
    out = search_resolve({"text": "debuging", "models": ["testapp.party"]})
    [(label, match, confidence)] = picks(out["objects"], "label", "match", "confidence")
    assert (label, match) == ("Debugging", "fuzzy")
    assert 0.3 < confidence < 0.6  # 0.6 x word_similarity
    assert out["objects"]["status"] == "ambiguous"  # a single weak candidate asks


def test_trigram_threshold(search_resolve, parties):
    # word_similarity('jhon', 'john doe') is 0.2, under the 0.4 threshold
    assert search_resolve({"text": "jhon", "models": ["testapp.party"]})["objects"]["status"] == "none"


def test_trigram_pass_only_fills_up_to_the_limit(search_resolve, parties):
    out = search_resolve({"text": "john", "models": ["testapp.party"]}, limit=1)
    assert [c["match"] for c in out["objects"]["candidates"]] == ["prefix"]
    assert out["objects"]["more"] is True


def test_arabic_trigrams_under_a_utf8_ctype(parties):
    assert db_engine.database_ctype() not in db_engine.UNSAFE_CTYPES
    assert query("SELECT word_similarity(%s, %s)", ["اسلا", "اسلام"])[0][0] > 0


def test_ctype_check(db, monkeypatch):
    assert check_engine(databases=["default"]) == []
    monkeypatch.setattr(db_engine, "database_ctype", lambda: "POSIX")
    [warning] = check_engine(databases=["default"])
    assert warning.id == "admin_api_search.W001"
    assert check_engine(databases=None) == []
