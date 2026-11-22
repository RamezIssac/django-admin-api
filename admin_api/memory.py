"""Per-user MCP memory: the preferences stored in :class:`admin_api.models.McpUserMemory`.

Preference keys: ``audience`` (``business`` or ``developer``), ``language`` (a ``settings.LANGUAGES``
code) and ``notes`` (free text for the agent, at most :data:`NOTES_MAX_LENGTH` characters).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.utils import translation
from django.utils.translation import gettext

from .errors import InvalidArguments

BUSINESS = "business"
DEVELOPER = "developer"
AUDIENCES = (BUSINESS, DEVELOPER)
NOTES_MAX_LENGTH = 500
PREFERENCE_KEYS = ("audience", "language", "notes")


@dataclass(frozen=True)
class UserMemory:
    """One user's preferences, defaults filled in."""

    audience: str = BUSINESS
    language: str | None = None
    notes: str = ""


def memory_model():
    from .models import McpUserMemory

    return McpUserMemory


def load_user_memory(user) -> UserMemory:
    """``user``'s stored preferences (one query); defaults for anonymous users and missing rows."""
    if user is None or not user.is_authenticated:
        return UserMemory()
    model = memory_model()
    rows = model.objects.filter(user=user, kind=model.PREFERENCE, key__in=PREFERENCE_KEYS)
    stored = {}
    for key, value in rows.values_list("key", "value"):
        try:
            value = clean_preference(key, value)
        except InvalidArguments:
            continue  # a row edited by hand into an invalid value falls back to the default
        if value is not None:
            stored[key] = value
    return UserMemory(**stored)


def clean_preference(key: str, value):
    """The stored form of ``value`` for preference ``key``; ``None`` clears it. Raises
    :class:`InvalidArguments` for an unknown key or an invalid value."""
    if key not in PREFERENCE_KEYS:
        raise InvalidArguments(
            gettext("Unknown preference. Use one of: %(keys)s.") % {"keys": ", ".join(PREFERENCE_KEYS)}
        )
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise InvalidArguments(gettext("The value must be text."))
    if key == "audience":
        if value not in AUDIENCES:
            raise InvalidArguments(gettext("Audience is one of: %(values)s.") % {"values": ", ".join(AUDIENCES)})
        return value
    if key == "language":
        try:
            return translation.get_supported_language_variant(value)
        except LookupError:
            codes = ", ".join(code for code, _name in settings.LANGUAGES)
            raise InvalidArguments(gettext("Language is one of: %(codes)s.") % {"codes": codes}) from None
    value = value.strip()
    if len(value) > NOTES_MAX_LENGTH:
        raise InvalidArguments(gettext("Notes are at most %(max)d characters.") % {"max": NOTES_MAX_LENGTH})
    return value or None


def set_preference(user, key: str, value) -> UserMemory:
    """Store (or with ``None``, clear) one of ``user``'s preferences; returns the new memory."""
    value = clean_preference(key, value)
    model = memory_model()
    rows = model.objects.filter(user=user, kind=model.PREFERENCE, key=key)
    if value is None:
        rows.delete()
    else:
        model.objects.update_or_create(user=user, kind=model.PREFERENCE, key=key, defaults={"value": value})
    return load_user_memory(user)
