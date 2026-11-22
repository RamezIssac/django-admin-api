"""The ``resolve_entities`` backend over the configured search engine: scopes from the tool's allowed
targets, candidates from the engine, scores from the tool's own match rules (see docs/DESIGN.md)."""

from __future__ import annotations

from ...mcp.resolve import CODE_FIELDS, CONFIDENCE, DEFAULT_LIMIT, Hit, _languages, match_text
from .conf import get_engine
from .engines.base import MODEL, OBJECT, Scope, SearchQuery
from .fold import fold
from .indexer import content_type_of, local_fields, model_names, owned_models

FUZZY_WEIGHT = 0.6  # a fuzzy hit's confidence is the engine's own score (0 to 1) times this


class SearchEntityBackend:
    """Finds ``resolve_entities`` candidates in the search index (``admin_api.contrib.search``)."""

    post_filter_factor = 5
    post_filter_pages = 3

    def __init__(self, site):
        self.site = site
        self.engine = get_engine()

    def _score(self, term: str, hit, code: bool) -> tuple[str, float]:
        match = match_text(term, hit.norm, code=code)
        if match:
            return match, CONFIDENCE[match]
        return "fuzzy", round(FUZZY_WEIGHT * hit.engine_score, 2)

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
            codes = CODE_FIELDS | frozenset(getattr(target.admin, "search_index_code_fields", None) or ())
            owned = owned_models(target.model, registered)
            scope = Scope([content_type_of(m) for m in owned], fields, codes, target.queryset)
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
        content_types = [content_type_of(t.model) for t in targets]
        by_ct = {ct.pk: t for ct, t in zip(content_types, targets)}
        if not wanted or not by_ct:
            return []
        scope = Scope(content_types)
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
        for _field, name in model_names(target.model, [target.admin], language):
            if fold(name) == norm:
                return name, language
    return norm, None
