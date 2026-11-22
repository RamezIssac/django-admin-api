"""``resolve_entities``: turn the names a user typed into object ids and model labels.

The tool owns the contract, the allowed set (exposure, permissions, token scopes, ``excluded_models``,
the admin's live search fields), the match and confidence rules and the ``status`` rule. Finding
candidates is the job of the site's entity backend (``AdminMcpSite.get_entity_backend()``); the
default :class:`LiveEntityBackend` runs each admin's own ``get_search_results`` over its
``get_queryset(request)``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from django import forms
from django.conf import settings
from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db.models import Q
from django.utils import translation

from .. import scopes
from ..detail import inline_prefixes
from ..errors import InvalidArguments, NotFound

DEFAULT_LIMIT = 5
MAX_LIMIT = 20
MAX_TERMS = 20
KINDS = ("any", "objects", "models")
INLINE_FIELD_PREFIX = "inlines."

# match type -> confidence
CONFIDENCE = {
    "exact": 1.0,
    "exact_code": 0.95,
    "prefix": 0.85,
    "word": 0.8,
    "word_prefix": 0.75,
    "contains": 0.6,
    "search": 0.5,  # found by the admin's search, but no single field holds the whole term
}
EXACT_MATCHES = ("exact", "exact_code")
CONFIDENT = 0.7
CODE_FIELDS = frozenset({"slug", "code", "reference", "ref_number"})


def normalize(text) -> str:
    return " ".join(str(text).casefold().split())


def match_text(term: str, value, *, code: bool = False) -> str | None:
    """The match type of the normalized ``term`` in ``value``, or ``None``. ``code`` marks
    identifier fields, whose exact match is ``exact_code``."""
    value = normalize(value)
    if not value or not term:
        return None
    if value == term:
        return "exact_code" if code else "exact"
    if value.startswith(term):
        return "prefix"
    if re.search(rf"(^| ){re.escape(term)}( |$)", value):
        return "word"
    if f" {term}" in value:
        return "word_prefix"
    if term in value:
        return "contains"
    return None


def list_status(hits) -> str:
    """``none``, ``unique`` or ``ambiguous`` for one list of hits."""
    if not hits:
        return "none"
    exact = [h for h in hits if h.match in EXACT_MATCHES]
    if exact:
        return "unique" if len(exact) == 1 else "ambiguous"
    return "unique" if sum(1 for h in hits if h.confidence >= CONFIDENT) == 1 else "ambiguous"


# --------------------------------------------------------------------------------------------
# allowed set
# --------------------------------------------------------------------------------------------
@dataclass
class Target:
    """A model the request may resolve against: its API admin, the scoped queryset the candidates
    must come from, and the admin's live search fields."""

    model: type[models.Model]
    label: str
    admin: object
    queryset: models.QuerySet
    search_fields: tuple
    to_field: str | None = None


@dataclass
class Hit:
    """One candidate found by a backend: an object (``pk``) or a model (``label``, ``language``)."""

    target: Target
    pk: object = None
    match: str = "search"
    confidence: float = CONFIDENCE["search"]
    matched_field: str | None = None
    label: str | None = None
    language: str | None = None


def is_excluded(site, model) -> bool:
    excluded = site.excluded_models or ()
    return model._meta.app_label in excluded or model._meta.label_lower in excluded


def allowed_targets(site, request) -> dict[str, Target]:
    """``{label: Target}`` of the top-level admins this request may resolve against."""
    out = {}
    for model, model_admin in site.api_site.exposed_admins():
        if (
            not site.is_exposed(model_admin)
            or is_excluded(site, model)
            or not model_admin.has_view_or_change_permission(request)
            or not scopes.request_allows(request, model._meta, "view")
        ):
            continue
        label = model._meta.label_lower
        out[label] = Target(
            model=model,
            label=label,
            admin=model_admin,
            queryset=model_admin.get_queryset(request),
            search_fields=tuple(model_admin.get_search_fields(request) or ()),
        )
    return out


def _form_field(source: Target, request, field: str):
    """The form field behind ``field`` of the source admin's add form, or of an inline row form
    for ``inlines.<prefix>.<field>``."""
    if field.startswith(INLINE_FIELD_PREFIX):
        prefix, _dot, name = field[len(INLINE_FIELD_PREFIX) :].rpartition(".")
        inlines = {p: FormSet for p, FormSet, _inline in inline_prefixes(source.admin, request, None, False)}
        if prefix not in inlines:
            raise InvalidArguments(f"No inline {prefix!r} on {source.label}. Inlines: {', '.join(inlines) or 'none'}.")
        fields = inlines[prefix].form.base_fields
    else:
        name = field
        fields = source.admin.get_form(request, None, change=False).base_fields
    form_field = fields.get(name)
    if not isinstance(form_field, forms.ModelChoiceField):
        raise InvalidArguments(f"{field!r} is not a relation field of {source.label}'s form.")
    return form_field


def for_field_target(targets: dict[str, Target], request, for_field) -> Target:
    """The target of a create/update form field: the related admin's queryset narrowed by the
    field's ``limit_choices_to``, as Django's autocomplete does."""
    if not isinstance(for_field, dict) or not isinstance(for_field.get("model"), str) or not for_field.get("field"):
        raise InvalidArguments("'for_field' must be an object with 'model' and 'field'.")
    source = targets.get(for_field["model"])
    if source is None:
        raise NotFound()
    form_field = _form_field(source, request, str(for_field["field"]))
    related = targets.get(form_field.queryset.model._meta.label_lower)
    if related is None:
        raise NotFound()
    limit = form_field.get_limit_choices_to()
    queryset = related.queryset.complex_filter(limit) if limit else related.queryset
    return replace(related, queryset=queryset, to_field=form_field.to_field_name)


# --------------------------------------------------------------------------------------------
# the default backend
# --------------------------------------------------------------------------------------------
def _value_path(model, search_field: str) -> str | None:
    """The ORM path of a search field's value (``^name`` -> ``name``, ``client__name__iexact`` ->
    ``client__name``), or ``None`` when it does not end on a concrete non-relation field."""
    path, current, field = [], model, None
    for part in search_field.lstrip("^=@").split("__"):
        if current is None:
            break
        try:
            field = current._meta.pk if part == "pk" else current._meta.get_field(part)
        except FieldDoesNotExist:
            break
        path.append(part)
        current = field.related_model if field.is_relation else None
    if not path or field is None or field.is_relation or not field.concrete:
        return None
    return "__".join(path)


class LiveEntityBackend:
    """Finds candidates with each admin's own search (``get_search_results`` over its
    ``get_queryset(request)``) on any database. Needs no index; scores at most ``scan_limit``
    rows per model, exact matches first."""

    scan_limit = 200

    def __init__(self, site):
        self.site = site

    def search_objects(self, request, term: str, targets: list[Target], limit=None) -> tuple[list[Hit], bool]:
        """All scored hits, best first, and whether a model had more rows than were scanned. A
        backend may return only the best ``limit + 1``; this one ignores ``limit``."""
        hits, capped = [], False
        for target in targets:
            found, more = self._search_target(request, term, target)
            hits.extend(found)
            capped = capped or more
        hits.sort(key=lambda h: -h.confidence)
        return hits, capped

    def _search_target(self, request, term: str, target: Target) -> tuple[list[Hit], bool]:
        paths = list(dict.fromkeys(filter(None, (_value_path(target.model, f) for f in target.search_fields))))
        qs, may_have_duplicates = target.admin.get_search_results(request, target.queryset, term)
        if may_have_duplicates:
            qs = target.queryset.filter(pk__in=qs.values("pk"))
        ordering = [o for o in (target.admin.get_ordering(request) or ()) if isinstance(o, str)]
        qs = qs.order_by(*ordering, "-pk")
        pks = []
        if paths:
            exact = Q()
            for path in paths:
                exact |= Q(**{f"{path}__iexact": term})
            pks.extend(qs.filter(exact).values_list("pk", flat=True)[: self.scan_limit])
        scanned = list(qs.values_list("pk", flat=True)[: self.scan_limit + 1])
        capped = len(scanned) > self.scan_limit

        best: dict = {pk: Hit(target, pk) for pk in pks + scanned[: self.scan_limit]}
        wanted = normalize(term)
        if paths and best:
            for pk, *values in target.queryset.filter(pk__in=list(best)).values_list("pk", *paths):
                hit = best[pk]
                for path, value in zip(paths, values):
                    code = path.rsplit("__", 1)[-1] in CODE_FIELDS
                    match = match_text(wanted, value, code=code) if value is not None else None
                    if match and CONFIDENCE[match] > hit.confidence:
                        hit.match, hit.confidence, hit.matched_field = match, CONFIDENCE[match], path
        return list(best.values()), capped

    def search_models(self, request, term: str, targets: list[Target], limit=None) -> list[Hit]:
        """Models whose verbose name, plural or model name matches, in each language."""
        wanted, languages, hits = normalize(term), _languages(), []
        for target in targets:
            best = None
            for language in languages:
                with translation.override(language):
                    opts = target.model._meta
                    names = (str(opts.verbose_name), str(opts.verbose_name_plural), opts.model_name)
                for name in names:
                    match = match_text(wanted, name)
                    if match and (best is None or CONFIDENCE[match] > best.confidence):
                        best = Hit(target, match=match, confidence=CONFIDENCE[match], label=name, language=language)
            if best is not None:
                hits.append(best)
        hits.sort(key=lambda h: -h.confidence)
        return hits


def _languages() -> list[str]:
    """Every ``settings.LANGUAGES`` code when the project sets it, else the active language and
    ``LANGUAGE_CODE``."""
    if settings.is_overridden("LANGUAGES"):
        codes = [code for code, _name in settings.LANGUAGES]
    else:
        codes = [translation.get_language() or settings.LANGUAGE_CODE, settings.LANGUAGE_CODE]
    return list(dict.fromkeys(codes))


# --------------------------------------------------------------------------------------------
# the tool
# --------------------------------------------------------------------------------------------
def _object_candidates(hits: list[Hit]) -> list[dict]:
    """Candidate dicts for ``hits``, labels read from each target's scoped queryset."""
    pks_by_target: dict = {}
    for hit in hits:
        pks_by_target.setdefault(hit.target.label, (hit.target, []))[1].append(hit.pk)
    objects = {
        (target.label, obj.pk): obj
        for target, pks in pks_by_target.values()
        for obj in target.queryset.filter(pk__in=pks)
    }
    out = []
    for hit in hits:
        obj = objects.get((hit.target.label, hit.pk))
        if obj is None:
            continue
        object_id = getattr(obj, hit.target.to_field) if hit.target.to_field else obj.pk
        if not isinstance(object_id, (int, str)) or isinstance(object_id, bool):
            object_id = str(object_id)
        out.append(
            {
                "model": hit.target.label,
                "id": object_id,
                "label": str(obj),
                "confidence": hit.confidence,
                "match": hit.match,
                "matched_field": hit.matched_field,
            }
        )
    return out


def _resolve_objects(backend, request, term: str, targets: list[Target], limit: int, verbose: bool) -> dict:
    hits, capped = backend.search_objects(request, term, targets, limit=limit)
    status = list_status(hits)
    out = {"status": status, "more": capped or len(hits) > limit, "candidates": _object_candidates(hits[:limit])}
    if verbose or status == "none":
        out["searched_models"] = [t.label for t in targets]
    return out


def _resolve_models(backend, request, term: str, targets: list[Target], limit: int) -> dict:
    hits = backend.search_models(request, term, targets, limit=limit)
    return {
        "status": list_status(hits),
        "more": len(hits) > limit,
        "candidates": [
            {
                "model": h.target.label,
                "label": h.label,
                "language": h.language,
                "confidence": h.confidence,
                "match": h.match,
            }
            for h in hits[:limit]
        ],
    }


def _check_labels(value):
    if value is not None and (not isinstance(value, list) or not all(isinstance(m, str) for m in value)):
        raise InvalidArguments("'models' must be a list of model labels.")


def _resolve_term(
    backend, request, targets: dict[str, Target], term: dict, limit: int, default_models, verbose
) -> dict:
    if not isinstance(term, dict):
        raise InvalidArguments("Each entry of 'terms' must be an object with 'text'.")
    text = term.get("text")
    if not isinstance(text, str) or not text.strip():
        raise InvalidArguments("Each term needs a non-empty 'text'.")
    text = text.strip()
    models_arg, for_field = term.get("models"), term.get("for_field")
    kind = term.get("kind", "any")
    if kind not in KINDS:
        raise InvalidArguments(f"'kind' must be one of {', '.join(KINDS)}.")
    if models_arg is None and for_field is None and kind != "models":
        models_arg = default_models
    _check_labels(models_arg)
    if models_arg is not None or for_field is not None:
        if kind == "models":
            raise InvalidArguments(
                "'models' and 'for_field' narrow an object search; they do not go with kind='models'."
            )
        kind = "objects"

    out: dict = {"term": text}
    if kind in ("any", "objects"):
        if for_field is not None:
            object_targets = [for_field_target(targets, request, for_field)]
        elif models_arg is not None:
            object_targets = [targets[m] for m in dict.fromkeys(models_arg) if m in targets]
        else:
            object_targets = list(targets.values())
        object_targets = [t for t in object_targets if t.search_fields]
        out["objects"] = _resolve_objects(backend, request, text, object_targets, limit, verbose)
    if kind in ("any", "models"):
        out["models"] = _resolve_models(backend, request, text, list(targets.values()), limit)
    return out


def resolve_entities(site, request, args) -> dict:
    terms = args.get("terms")
    if not isinstance(terms, list) or not terms:
        raise InvalidArguments("'terms' must be a list of at least one term.")
    if len(terms) > MAX_TERMS:
        raise InvalidArguments(f"At most {MAX_TERMS} terms per call.")
    limit = args.get("limit", DEFAULT_LIMIT)
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise InvalidArguments("'limit' must be an integer.")
    limit = max(1, min(limit, MAX_LIMIT))
    verbose = args.get("verbose", False)
    if not isinstance(verbose, bool):
        raise InvalidArguments("'verbose' must be a boolean.")
    targets = allowed_targets(site, request)
    default_models = args.get("models")
    _check_labels(default_models)
    backend = site.get_entity_backend()
    return {"results": [_resolve_term(backend, request, targets, t, limit, default_models, verbose) for t in terms]}
