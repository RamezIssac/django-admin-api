"""The ``ADMIN_API_SEARCH`` setting, merged over the defaults, and the configured engine."""

from __future__ import annotations

from functools import cache

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver
from django.utils.module_loading import import_string

DB_ENGINE = "admin_api.contrib.search.engines.db.DatabaseEngine"
DEFAULTS = {
    "DEFAULT_ENGINE": "db",
    "ENGINES": {"db": {"BACKEND": DB_ENGINE}},
}


def search_settings() -> dict:
    user = getattr(settings, "ADMIN_API_SEARCH", None) or {}
    return {
        "DEFAULT_ENGINE": user.get("DEFAULT_ENGINE", DEFAULTS["DEFAULT_ENGINE"]),
        "ENGINES": {**DEFAULTS["ENGINES"], **user.get("ENGINES", {})},
    }


@cache
def get_engine():
    """The engine named by ``DEFAULT_ENGINE``, built once per process."""
    conf = search_settings()
    name = conf["DEFAULT_ENGINE"]
    spec = conf["ENGINES"].get(name)
    if not spec or not spec.get("BACKEND"):
        raise ImproperlyConfigured(f"ADMIN_API_SEARCH: no engine {name!r} with a BACKEND in ENGINES.")
    return import_string(spec["BACKEND"])(name, spec.get("OPTIONS"))


@receiver(setting_changed)
def _reset(setting, **kwargs):
    if setting == "ADMIN_API_SEARCH":
        get_engine.cache_clear()
