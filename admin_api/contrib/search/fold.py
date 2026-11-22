"""``fold()``: the one normalization applied to indexed text and to the search term."""

from __future__ import annotations

import unicodedata

ARABIC_MAP = str.maketrans(
    {
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ٱ": "ا",
        "ى": "ي",
        "ة": "ه",
        "ؤ": "و",
        "ئ": "ي",
        "ـ": None,  # tatweel
        "ک": "ك",  # Persian kaf
        "ی": "ي",  # Persian yeh
        **{chr(0x0660 + d): str(d) for d in range(10)},  # Arabic-Indic digits
        **{chr(0x06F0 + d): str(d) for d in range(10)},  # Persian digits
    }
)
ARTICLE = "ال"


def _strip_article(word: str) -> str:
    if word.startswith(ARTICLE) and len(word) - len(ARTICLE) >= 2:
        return word[len(ARTICLE) :]
    return word


def fold(text) -> str:
    """Folded ``text``: NFKC, Arabic letter and digit variants unified, combining marks dropped,
    casefolded, whitespace collapsed, and a leading "ال" stripped from each Arabic word."""
    if text is None:
        return ""
    value = unicodedata.normalize("NFKC", str(text)).translate(ARABIC_MAP)
    value = "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))
    value = unicodedata.normalize("NFC", value).casefold()
    return " ".join(_strip_article(word) for word in value.split())
