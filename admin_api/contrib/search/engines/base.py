"""The interface every search engine implements, and the values it trades with the contrib core.

The core decides which rows exist (:mod:`..indexer`), which rows a request may see and how a hit
scores (:mod:`..backend`). An engine stores rows and finds candidates.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.contrib.contenttypes.models import ContentType
from django.db.models import QuerySet

OBJECT = "object"
MODEL = "model"


@dataclass(frozen=True)
class Entry:
    """One row to store: an object's search field (``kind="object"``) or a model label under one
    language (``kind="model"``, ``object_pk=None``)."""

    kind: str
    content_type: ContentType
    object_pk: str | None
    field: str
    language: str
    text: str
    norm: str


@dataclass(frozen=True)
class Hit:
    """One stored row an engine found. ``engine_score`` is the engine's own relevance."""

    content_type: ContentType
    object_pk: str | None
    field: str
    norm: str
    engine_score: float = 0.0


@dataclass
class Scope:
    """Rows of these content types and fields that the request may see. ``queryset`` is the admin's
    ``get_queryset(request)`` (narrowed for ``for_field``); engines with ``scope_pushdown`` filter
    by it, the others leave it to the core."""

    content_types: list[ContentType]
    fields: tuple[str, ...] = ()
    code_fields: frozenset = frozenset()
    queryset: QuerySet | None = None


@dataclass
class SearchQuery:
    """``text`` is folded. ``limit`` counts distinct candidates (objects or models), ``offset``
    skips that many for the core's post-filter paging."""

    text: str
    kind: str
    scopes: list[Scope] = field(default_factory=list)
    limit: int = 6
    offset: int = 0


class SearchEngine:
    """Base class of search engines, configured under ``ADMIN_API_SEARCH["ENGINES"]``.

    ``fuzzy``: the engine finds hits beyond exact, prefix, word and contains matches.
    ``scope_pushdown``: :meth:`search` applies each scope's ``queryset`` itself.
    """

    fuzzy = False
    scope_pushdown = False

    def __init__(self, name: str = "", options: dict | None = None):
        self.name = name
        self.options = dict(options or {})

    def upsert(self, entries: list[Entry]) -> None:
        """Insert object rows, or update the stored ones with the same (content type, pk, field)."""
        raise NotImplementedError

    def delete_rows(self, content_type: ContentType, object_pk: str, fields=None) -> None:
        """Delete an object's rows, only those of ``fields`` when given."""
        raise NotImplementedError

    def replace_model_rows(self, rows: list[Entry]) -> None:
        """Replace every stored model row with ``rows``."""
        raise NotImplementedError

    def prune(self, content_types: list[ContentType], older_than) -> int:
        """Delete the object rows of ``content_types`` last written before ``older_than``."""
        raise NotImplementedError

    def search(self, query: SearchQuery) -> list[Hit]:
        """Rows of the best ``query.limit`` candidates after ``query.offset``, best first."""
        raise NotImplementedError

    def fuzzy_confidence(self, hit: Hit) -> float:
        """0 to 1 for a hit that is not an exact, prefix, word or contains match."""
        return 0.0

    def check(self) -> list:
        """System check messages about this engine's setup."""
        return []
