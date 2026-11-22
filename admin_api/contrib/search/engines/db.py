"""The built-in ``db`` engine: rows in the ``admin_api_search_entry`` table, scoped inside the query
by an ``EXISTS`` against each admin's ``get_queryset(request)``. Pass 1 matches ``norm`` exactly, by
prefix, word or substring; pass 2 (Postgres with ``pg_trgm``) adds ``word_similarity`` hits.
"""

from __future__ import annotations

from functools import reduce
from operator import or_

from django.core import checks
from django.db import connection, models, transaction
from django.db.models import Case, Exists, F, FloatField, Max, OuterRef, Q, Value, When
from django.db.models.expressions import RawSQL
from django.db.models.functions import Cast, Lower, Replace
from django.utils import timezone

from ....mcp.resolve import CONFIDENCE
from .base import MODEL, OBJECT, Hit, SearchEngine, SearchQuery

UNSAFE_CTYPES = ("C", "POSIX")
UPSERT_VENDORS = ("postgresql", "sqlite")
UPSERT_BATCH = 500


def database_ctype() -> str | None:
    """``LC_CTYPE`` of the default database on Postgres, else ``None``."""
    if connection.vendor != "postgresql":
        return None
    with connection.cursor() as cursor:
        cursor.execute("SELECT datctype FROM pg_database WHERE datname = current_database()")
        return cursor.fetchone()[0]


def _any(conditions) -> Q:
    conditions = list(conditions)
    return reduce(or_, conditions) if conditions else Q(pk__in=[])


def _pk_field(model):
    field = model._meta.pk
    while field.is_relation:  # multi-table inheritance: the parent link
        field = field.target_field
    return field


class DatabaseEngine(SearchEngine):
    scope_pushdown = True

    def __init__(self, name: str = "db", options: dict | None = None):
        super().__init__(name, options)
        self.threshold = float(self.options.get("TRIGRAM_THRESHOLD", 0.4))
        self._trigram = None

    @property
    def model(self):
        from ..models import SearchEntry

        return SearchEntry

    @property
    def fuzzy(self) -> bool:
        """Postgres with ``pg_trgm`` installed (checked once per process)."""
        if self._trigram is None:
            self._trigram = False
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
                    self._trigram = cursor.fetchone() is not None
        return self._trigram

    # --- writes ---------------------------------------------------------------------------------
    def upsert(self, entries) -> None:
        rows = list({(e.content_type.pk, e.object_pk, e.field, e.language): e for e in entries}.values())
        if not rows:
            return
        now = timezone.now()
        if connection.vendor not in UPSERT_VENDORS:
            self._upsert_portable(rows, now)
            return
        table = connection.ops.quote_name(self.model._meta.db_table)
        columns = "kind, content_type_id, object_pk, field, language, text, norm, updated_at, indexed_at"
        stamp = connection.ops.adapt_datetimefield_value(now)
        with connection.cursor() as cursor:
            for start in range(0, len(rows), UPSERT_BATCH):
                chunk = rows[start : start + UPSERT_BATCH]
                values = ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s, %s)"] * len(chunk))
                params = []
                for e in chunk:
                    params += [
                        e.kind,
                        e.content_type.pk,
                        e.object_pk,
                        e.field,
                        e.language,
                        e.text,
                        e.norm,
                        stamp,
                        stamp,
                    ]
                cursor.execute(
                    f"INSERT INTO {table} ({columns}) VALUES {values} "
                    "ON CONFLICT (content_type_id, object_pk, field, language) DO UPDATE SET "
                    "kind = excluded.kind, text = excluded.text, "
                    f"updated_at = CASE WHEN {table}.norm = excluded.norm THEN {table}.updated_at "
                    "ELSE excluded.updated_at END, "
                    "norm = excluded.norm, indexed_at = excluded.indexed_at",
                    params,
                )

    def _upsert_portable(self, rows, now) -> None:
        for e in rows:
            current = self.model.objects.filter(
                content_type=e.content_type, object_pk=e.object_pk, field=e.field, language=e.language
            ).first()
            if current is None:
                self.model.objects.create(**vars(e), updated_at=now, indexed_at=now)
                continue
            if current.norm != e.norm:
                current.updated_at = now
            current.kind, current.text, current.norm, current.indexed_at = e.kind, e.text, e.norm, now
            current.save()

    def delete_rows(self, content_type, object_pk, fields) -> None:
        self.model.objects.filter(content_type=content_type, object_pk=object_pk, field__in=list(fields)).delete()

    def replace_model_rows(self, rows) -> None:
        now = timezone.now()
        with transaction.atomic():
            self.model.objects.filter(kind=MODEL).delete()
            self.model.objects.bulk_create(
                [self.model(**vars(e), updated_at=now, indexed_at=now) for e in rows], batch_size=UPSERT_BATCH
            )

    def prune(self, content_types, older_than) -> int:
        rows = self.model.objects.filter(kind=OBJECT, content_type__in=list(content_types), indexed_at__lt=older_than)
        return rows.delete()[0]

    # --- search ---------------------------------------------------------------------------------
    def _scoped(self, query: SearchQuery):
        """Rows of ``query.kind`` the scopes allow, with the ``EXISTS`` checks for object rows."""
        rows, conditions, aliases = self.model.objects.filter(kind=query.kind), [], {}
        for i, scope in enumerate(query.scopes):
            ct_ids = [ct.pk for ct in scope.content_types]
            condition = Q(content_type_id__in=ct_ids)
            if query.kind == OBJECT:
                condition &= Q(field__in=list(scope.fields))
                condition &= self._exists(scope.queryset, ct_ids, f"_pk{i}", aliases)
            conditions.append(condition)
        return rows.alias(**aliases).filter(_any(conditions))

    @staticmethod
    def _exists(queryset, ct_ids, alias, aliases):
        """``EXISTS`` of the row's object in ``queryset``. The text pk is cast on the outer side
        (the inner pk index stays usable), guarded by the content type."""
        pk = _pk_field(queryset.model)
        if isinstance(pk, models.IntegerField):
            target = models.BigIntegerField()
        elif isinstance(pk, models.UUIDField) and connection.vendor == "postgresql":
            target = models.UUIDField()
        elif isinstance(pk, models.UUIDField):  # stored as 32 hex digits outside Postgres
            aliases[alias] = Case(
                When(content_type_id__in=ct_ids, then=Lower(Replace(F("object_pk"), Value("-"), Value(""))))
            )
            return Exists(queryset.filter(pk=OuterRef(alias)))
        elif isinstance(pk, (models.CharField, models.TextField)):
            return Exists(queryset.filter(pk=OuterRef("object_pk")))
        else:
            inner = queryset.alias(_pk_text=Cast("pk", models.TextField()))
            return Exists(inner.filter(_pk_text=OuterRef("object_pk")))
        aliases[alias] = Case(When(content_type_id__in=ct_ids, then=Cast("object_pk", target)))
        return Exists(queryset.filter(pk=OuterRef(alias)))

    @staticmethod
    def _rank(query: SearchQuery):
        q = query.text
        code = _any(
            Q(content_type_id__in=[ct.pk for ct in s.content_types], field__in=list(s.code_fields))
            for s in query.scopes
            if s.code_fields
        )
        word = Q(norm__startswith=f"{q} ") | Q(norm__endswith=f" {q}") | Q(norm__contains=f" {q} ")
        return Case(
            When(Q(norm=q) & code, then=Value(CONFIDENCE["exact_code"])),
            When(norm=q, then=Value(CONFIDENCE["exact"])),
            When(norm__startswith=q, then=Value(CONFIDENCE["prefix"])),
            When(word, then=Value(CONFIDENCE["word"])),
            When(norm__contains=f" {q}", then=Value(CONFIDENCE["word_prefix"])),
            default=Value(CONFIDENCE["contains"]),
            output_field=FloatField(),
        )

    @staticmethod
    def _matches(q: str) -> Q:
        # terms under 3 characters: exact and prefix only (the trigram index needs 3)
        return Q(norm__contains=q) if len(q) >= 3 else Q(norm=q) | Q(norm__startswith=q)

    @staticmethod
    def _top(rows, score, limit: int, offset: int = 0) -> list[tuple]:
        top = (
            rows.values("content_type_id", "object_pk")
            .annotate(best=Max(score), seen=Max("updated_at"))
            .order_by("-best", "-seen", "content_type_id", "object_pk")[offset : offset + limit]
        )
        return [(r["content_type_id"], r["object_pk"]) for r in top]

    @staticmethod
    def _rows_of(rows, keys, score):
        """The rows of the candidates ``keys``, in candidate order, best row first."""
        if not keys:
            return []
        found = rows.filter(_any(Q(content_type_id=ct, object_pk=pk) for ct, pk in keys)).annotate(score=score)
        order = {key: i for i, key in enumerate(keys)}
        found = sorted(
            found.values("content_type_id", "object_pk", "field", "norm", "score"),
            key=lambda r: (order[(r["content_type_id"], r["object_pk"])], -r["score"]),
        )
        return found

    def search(self, query: SearchQuery) -> list[Hit]:
        from django.contrib.contenttypes.models import ContentType

        q = query.text
        if not q or not query.scopes:
            return []
        scoped = self._scoped(query)
        rank = self._rank(query)
        matched = scoped.filter(self._matches(q))
        keys = self._top(matched, rank, query.limit, query.offset)
        rows = self._rows_of(matched, keys, rank)
        if len(keys) < query.limit and self.fuzzy and len(q) >= 3:
            rows += self._fuzzy_rows(scoped, q, keys, query.limit - len(keys))
        return [
            Hit(
                content_type=ContentType.objects.get_for_id(r["content_type_id"]),
                object_pk=r["object_pk"],
                field=r["field"],
                norm=r["norm"],
                engine_score=float(r["score"]),
            )
            for r in rows
        ]

    def _fuzzy_rows(self, scoped, q: str, found_keys, limit: int) -> list[dict]:
        column = f"{connection.ops.quote_name(self.model._meta.db_table)}.{connection.ops.quote_name('norm')}"
        similar = RawSQL(f"%s <%% {column}", (q,), output_field=models.BooleanField())
        score = RawSQL(f"word_similarity(%s, {column})", (q,), output_field=FloatField())
        rows = scoped.filter(similar)
        if found_keys:
            rows = rows.exclude(_any(Q(content_type_id=ct, object_pk=pk) for ct, pk in found_keys))
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT set_config('pg_trgm.word_similarity_threshold', %s, true)", [str(self.threshold)])
            keys = self._top(rows, score, limit)
            return self._rows_of(rows, keys, score)

    def check(self) -> list:
        ctype = database_ctype()
        if ctype in UNSAFE_CTYPES:
            return [
                checks.Warning(
                    f"The database LC_CTYPE is {ctype!r}: pg_trgm finds no trigrams in non-ASCII words.",
                    hint="Create the database with a UTF-8 locale such as C.UTF-8 or en_US.UTF-8.",
                    id="admin_api_search.W001",
                )
            ]
        return []
