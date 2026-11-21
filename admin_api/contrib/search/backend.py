"""The ``resolve_entities`` backend over the configured search engine.

The core builds the scopes from the tool's allowed targets, asks the engine for candidates and
scores each hit from its ``norm`` with the tool's own match rules. Engines without
``scope_pushdown`` get the post-filter path: up to ``5 x limit`` hits per page, kept only when the
admin's ``get_queryset(request)`` holds the pk, at most 3 pages.
"""

from __future__ import annotations

from django.apps import apps
from django.utils import translation

from ...mcp.resolve import CONFIDENCE, DEFAULT_LIMIT, FUZZY_WEIGHT, Hit, _languages, match_text
from .conf import get_engine
from .engines.base import MODEL, OBJECT, Scope, SearchQuery
from .fold import fold
from .indexer import LABEL_FIELDS, code_fields, content_type_of, local_fields, nearest_model


class SearchEntityBackend:
    """Finds ``resolve_entities`` candidates in the search index (``admin_api.contrib.search``)."""

    post_filter_factor = 5
    post_filter_pages = 3

    def __init__(self, site, engine=None):
        self.site = site
        self.engine = engine or get_engine()

    def _score(self, term: str, hit, code: bool) -> tuple[str, float]:
        match = match_text(term, hit.norm, code=code)
        if match:
            return match, CONFIDENCE[match]
        return "fuzzy", round(FUZZY_WEIGHT * self.engine.fuzzy_confidence(hit), 2)

    # --- objects --------------------------------------------------------------------------------
    def _scopes(self, targets) -> dict:
        """``{content type id: (target, scope)}`` for the targets with indexed search fields."""
        registered = {m for m, a in self.site.api_site.exposed_admins() if self.site.is_exposed(a)}
        registered |= {t.model for t in targets}
        by_ct = {}
        for target in targets:
            fields = local_fields(target.model, target.search_fields)
            if not fields:
                continue
            owned = [target.model] + [
                m
                for m in apps.get_models()
                if m is not target.model
                and issubclass(m, target.model)
                and nearest_model(m, registered) is target.model
            ]
            scope = Scope([content_type_of(m) for m in owned], fields, code_fields(target.admin), target.queryset)
            for ct in scope.content_types:
                by_ct[ct.pk] = (target, scope)
        return by_ct

    def search_objects(self, request, term: str, targets, limit: int = DEFAULT_LIMIT):
        """The best ``limit + 1`` candidates, best first, and ``False`` (the index is never capped)."""
        wanted = fold(term)
        by_ct = self._scopes(targets)
        if not wanted or not by_ct:
            return [], False
        scopes = list({id(s): s for _t, s in by_ct.values()}.values())
        count = max(limit + 1, 2)  # two candidates decide between unique and ambiguous
        if self.engine.scope_pushdown:
            engine_hits = self.engine.search(SearchQuery(wanted, OBJECT, scopes, limit=count))
        else:
            engine_hits = self._post_filtered(wanted, scopes, by_ct, count)
        best: dict = {}
        for engine_hit in engine_hits:
            target, scope = by_ct.get(engine_hit.content_type.pk, (None, None))
            if target is None:
                continue
            match, confidence = self._score(wanted, engine_hit, engine_hit.field in scope.code_fields)
            pk = target.model._meta.pk.to_python(engine_hit.object_pk)
            key = (target.label, pk)
            if key not in best or confidence > best[key].confidence:
                best[key] = Hit(target, pk, match=match, confidence=confidence, matched_field=engine_hit.field)
        hits = sorted(best.values(), key=lambda h: -h.confidence)
        return hits[:count], False

    def _post_filtered(self, wanted, scopes, by_ct, count) -> list:
        """Engine hits whose objects the admin querysets hold, paging until ``count`` candidates."""
        page_size, kept, kept_keys = count * self.post_filter_factor, [], set()
        for page in range(self.post_filter_pages):
            hits = self.engine.search(SearchQuery(wanted, OBJECT, scopes, limit=page_size, offset=page * page_size))
            pks_by_target: dict = {}
            for hit in hits:
                target, _scope = by_ct.get(hit.content_type.pk, (None, None))
                if target is not None:
                    pks_by_target.setdefault(id(target), (target, set()))[1].add(hit.object_pk)
            visible = set()
            for target, pks in pks_by_target.values():
                found = target.queryset.filter(pk__in=[target.model._meta.pk.to_python(pk) for pk in pks])
                visible.update((target.label, str(pk)) for pk in found.values_list("pk", flat=True))
            for hit in hits:
                target, _scope = by_ct.get(hit.content_type.pk, (None, None))
                if target is not None and (target.label, hit.object_pk) in visible:
                    kept.append(hit)
                    kept_keys.add((target.label, hit.object_pk))
            if len(kept_keys) >= count or len({(h.content_type.pk, h.object_pk) for h in hits}) < page_size:
                break
        return kept

    # --- models ---------------------------------------------------------------------------------
    def search_models(self, request, term: str, targets, limit: int = DEFAULT_LIMIT) -> list[Hit]:
        wanted = fold(term)
        by_ct = {content_type_of(t.model).pk: t for t in targets}
        if not wanted or not by_ct:
            return []
        scope = Scope([content_type_of(t.model) for t in targets])
        engine_hits = self.engine.search(SearchQuery(wanted, MODEL, [scope], limit=max(limit + 1, 2)))
        best: dict = {}
        for engine_hit in engine_hits:
            target = by_ct.get(engine_hit.content_type.pk)
            if target is None:
                continue
            match, confidence = self._score(wanted, engine_hit, False)
            if target.label not in best or confidence > best[target.label].confidence:
                label, language = model_label_text(target, engine_hit.norm)
                best[target.label] = Hit(target, match=match, confidence=confidence, label=label, language=language)
        return sorted(best.values(), key=lambda h: -h.confidence)


def model_label_text(target, norm: str) -> tuple[str, str | None]:
    """The label string and language whose folded form is ``norm``, read live."""
    for language in _languages():
        with translation.override(language):
            names = [str(getattr(target.model._meta, f)) for f in LABEL_FIELDS]
        names += list((getattr(target.admin, "search_aliases", None) or {}).get(language, ()))
        for name in names:
            if fold(name) == norm:
                return name, language
    return norm, None
