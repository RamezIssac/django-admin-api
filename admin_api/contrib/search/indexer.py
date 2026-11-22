"""Which rows exist: object rows from the admins' search fields, model rows from ``Meta`` labels.

Indexed models are the admins exposed by every ``AdminMcpSite`` minus its ``excluded_models``.
Rows stay current through ``post_save`` (on commit), ``post_migrate`` (model rows) and the
batched rebuild behind ``rebuild_search_index`` and ``search_index ensure``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from functools import cached_property

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import FieldDoesNotExist
from django.db import DEFAULT_DB_ALIAS, models, transaction
from django.utils import timezone, translation

from ...mcp.resolve import CODE_FIELDS, _languages, is_excluded
from .conf import get_engine, search_settings
from .engines.base import MODEL, OBJECT, Entry
from .fold import fold

logger = logging.getLogger("admin_api.search")

# bump when fold() or the row derivation changes: `search_index ensure` then rebuilds every model
INDEX_FORMAT = 1
INDEXED_FIELD_TYPES = (models.CharField, models.TextField, models.IntegerField, models.DecimalField)
LABEL_FIELDS = ("verbose_name", "verbose_name_plural", "model_name")


def local_field_name(model, search_field: str) -> str | None:
    """The local field a search field reads (``^name`` and ``name__iexact`` -> ``name``), or ``None``
    for relation paths and fields whose values are not indexed (dates, booleans, relations)."""
    name = search_field.lstrip("^=@").split("__", 1)[0]
    try:
        field = model._meta.pk if name == "pk" else model._meta.get_field(name)
    except FieldDoesNotExist:
        return None
    if field.is_relation or not field.concrete or not isinstance(field, INDEXED_FIELD_TYPES):
        return None
    if "__" in search_field and field.get_lookup(search_field.split("__", 1)[1].split("__")[0]) is None:
        return None  # a transform or a path, not a lookup on this field
    return field.name


def local_fields(model, search_fields) -> tuple[str, ...]:
    return tuple(dict.fromkeys(f for f in (local_field_name(model, s) for s in search_fields or ()) if f))


def code_fields(model_admin) -> frozenset:
    return CODE_FIELDS | frozenset(getattr(model_admin, "search_index_code_fields", None) or ())


def nearest_model(model, registered) -> type[models.Model] | None:
    """``model`` or its nearest ancestor in ``registered`` (the admin a row of ``model`` belongs to)."""
    return next((m for m in model.__mro__ if m in registered), None)


def content_type_of(model) -> ContentType:
    return ContentType.objects.get_for_model(model, for_concrete_model=False)


def default_sites():
    """Every ``AdminMcpSite``, after the URLconf that creates them is imported."""
    from django.urls import get_resolver

    from ...mcp.sites import all_mcp_sites

    get_resolver().url_patterns  # noqa: B018
    return all_mcp_sites()


class Indexer:
    """Builds and writes the rows of the indexed models of ``sites`` (default: every MCP site)."""

    def __init__(self, sites=None, engine=None):
        self._sites = sites
        self._engine = engine

    @property
    def engine(self):
        return self._engine or get_engine()

    @cached_property
    def admins(self) -> dict:
        """``{model: [api admin, ...]}``, one admin per site that indexes the model."""
        out: dict = {}
        for site in default_sites() if self._sites is None else self._sites:
            for model, model_admin in site.api_site.exposed_admins():
                if site.is_exposed(model_admin) and not is_excluded(site, model):
                    out.setdefault(model, []).append(model_admin)
        return out

    def index_fields(self, model) -> tuple[str, ...]:
        """Union over the model's admins of ``get_search_index_fields()``, else the local fields of
        ``search_fields`` and ``api_search_fields``."""
        names: list = []
        for model_admin in self.admins.get(model, ()):
            hook = getattr(model_admin, "get_search_index_fields", None)
            if hook is not None:
                names.extend(hook())
            else:
                names.extend(getattr(model_admin, "search_fields", None) or ())
                names.extend(getattr(model_admin, "api_search_fields", None) or ())
        return local_fields(model, names)

    def admin_model(self, model):
        return nearest_model(model, self.admins)

    def owned_models(self, model) -> list:
        """``model`` and the models without an indexed admin whose rows belong to it."""
        return [m for m in apps.get_models() if m is model or (issubclass(m, model) and self.admin_model(m) is model)]

    # --- object rows --------------------------------------------------------------------------
    def row_target(self, obj):
        """``(content type, admin model)`` of ``obj``'s rows: the django-polymorphic leaf when the
        model has one, else the model of the admin the object belongs to."""
        ctype_id = getattr(obj, "polymorphic_ctype_id", None)
        leaf = ContentType.objects.get_for_id(ctype_id).model_class() if ctype_id else type(obj)
        admin_model = self.admin_model(leaf) if leaf else None
        if admin_model is None:
            return None, None
        return content_type_of(leaf if ctype_id else admin_model), admin_model

    def object_entries(self, obj, content_type, admin_model) -> tuple[list[Entry], list[str]]:
        """The rows of ``obj`` and the index fields it leaves empty."""
        entries, empty = [], []
        object_pk = str(obj.pk)
        for name in self.index_fields(admin_model):
            value = getattr(obj, obj._meta.get_field(name).attname, None)
            norm = fold(value) if value is not None else ""
            if not norm:
                empty.append(name)
                continue
            entries.append(Entry(OBJECT, content_type, object_pk, name, "", str(value), norm))
        return entries, empty

    def index_object(self, obj) -> None:
        content_type, admin_model = self.row_target(obj)
        if content_type is None:
            return
        entries, empty = self.object_entries(obj, content_type, admin_model)
        with transaction.atomic():
            if entries:
                self.engine.upsert(entries)
            if empty:
                self.engine.delete_rows(content_type, str(obj.pk), fields=empty)

    # --- model rows ---------------------------------------------------------------------------
    def model_entries(self) -> list[Entry]:
        """Each indexed model's verbose name, plural and model name under every language, plus the
        admin's ``search_aliases``; one row per (model, folded text)."""
        rows = []
        for model, model_admins in self.admins.items():
            content_type, seen = content_type_of(model), set()
            for language in _languages():
                with translation.override(language):
                    names = [(f, str(getattr(model._meta, f))) for f in LABEL_FIELDS]
                aliases = [
                    a for ma in model_admins for a in (getattr(ma, "search_aliases", None) or {}).get(language, ())
                ]
                names.extend((f"alias.{i}", str(alias)) for i, alias in enumerate(aliases))
                for field, text in names:
                    norm = fold(text)
                    if norm and norm not in seen:
                        seen.add(norm)
                        rows.append(Entry(MODEL, content_type, None, field, language, text, norm))
        return rows

    def refresh_model_rows(self) -> int:
        rows = self.model_entries()
        self.engine.replace_model_rows(rows)
        return len(rows)

    # --- rebuild --------------------------------------------------------------------------------
    def rebuild_model(self, model, *, batch_size: int = 1000, stdout=None) -> int:
        """Rewrite every row of ``model``, one transaction per batch, then prune rows this run did
        not write. Returns the number of objects processed."""
        started = timezone.now()
        owned_types = [content_type_of(m) for m in self.owned_models(model)]
        if not self.index_fields(model):
            self.engine.prune(owned_types, older_than=started)
            return 0
        queryset = model._base_manager.order_by("pk")
        total, done, label = queryset.count(), 0, model._meta.label_lower
        batch: list = []

        def flush():
            entries = []
            for obj in batch:
                content_type, admin_model = self.row_target(obj)
                if admin_model is model:  # a polymorphic leaf with its own admin is rebuilt there
                    entries.extend(self.object_entries(obj, content_type, admin_model)[0])
            with transaction.atomic():
                self.engine.upsert(entries)
            batch.clear()

        for obj in queryset.iterator(chunk_size=batch_size):
            batch.append(obj)
            done += 1
            if len(batch) >= batch_size or done == total:
                flush()
                if stdout:
                    stdout.write(f"processed {done}/{total} {label}")
                    getattr(stdout, "flush", lambda: None)()
        if batch:  # rows added while iterating
            flush()
        self.engine.prune(owned_types, older_than=started)
        return done

    def fingerprint(self, model) -> str:
        """What the rows of ``model`` derive from: its index fields, the languages, the engine."""
        conf = search_settings()
        engine = conf["DEFAULT_ENGINE"]
        payload = [
            INDEX_FORMAT,
            model._meta.label_lower,
            self.index_fields(model),
            _languages(),
            engine,
            conf["ENGINES"].get(engine, {}).get("BACKEND"),
        ]
        return hashlib.sha256(json.dumps(payload, default=str).encode()).hexdigest()

    def rebuild(self, labels=None, *, batch_size: int = 1000, force: bool = True, stdout=None) -> list[str]:
        """Rebuild the indexed models (``labels`` narrows them; without ``force``, only those whose
        fingerprint changed), record fingerprints and drop models no longer indexed. Returns the
        labels rebuilt."""
        from .models import SearchIndexState

        indexed = {m._meta.label_lower: m for m in self.admins}
        state = dict(SearchIndexState.objects.values_list("label", "fingerprint"))
        rebuilt = []
        for label, model in indexed.items():
            if labels is not None and label not in labels:
                continue
            fingerprint = self.fingerprint(model)
            if not force and state.get(label) == fingerprint:
                continue
            self.rebuild_model(model, batch_size=batch_size, stdout=stdout)
            SearchIndexState.objects.update_or_create(label=label, defaults={"fingerprint": fingerprint})
            rebuilt.append(label)
        if labels is None:
            self.drop_stale(set(state) - set(indexed), stdout=stdout)
        return rebuilt

    def drop_stale(self, labels, stdout=None) -> None:
        """Delete the rows and state of models that are no longer indexed."""
        from .models import SearchIndexState

        for label in sorted(labels):
            try:
                model = apps.get_model(label)
            except LookupError:
                model = None
            if model is not None:
                self.engine.prune([content_type_of(model)], older_than=timezone.now())
            SearchIndexState.objects.filter(label=label).delete()
            if stdout:
                stdout.write(f"dropped {label}")


_indexer: Indexer | None = None


def get_indexer() -> Indexer:
    global _indexer
    if _indexer is None:
        _indexer = Indexer()
    return _indexer


def set_indexer(indexer: Indexer | None) -> None:
    """Replace the indexer the signals use (``None``: rebuild it from every MCP site on next use)."""
    global _indexer
    _indexer = indexer


def _index_saved(indexer: Indexer, obj) -> None:
    try:
        indexer.index_object(obj)
    except Exception:
        logger.exception("search index: could not index %s %r", obj._meta.label_lower, obj.pk)


def on_post_save(sender, instance, raw=False, using=DEFAULT_DB_ALIAS, **kwargs):
    if raw:
        return
    try:
        indexer = get_indexer()
        if indexer.admin_model(sender) is None:
            return
    except Exception:
        logger.exception("search index: could not load the indexed models")
        return
    transaction.on_commit(lambda: _index_saved(indexer, instance), using=using)


def on_post_migrate(using=DEFAULT_DB_ALIAS, **kwargs):
    if using != DEFAULT_DB_ALIAS:
        return
    try:
        get_indexer().refresh_model_rows()
    except Exception:
        logger.exception("search index: could not refresh the model rows")
