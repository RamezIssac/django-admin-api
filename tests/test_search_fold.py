"""fold(): the normalization shared by indexed text and the search term."""

import pytest

from admin_api.contrib.search.fold import fold


@pytest.mark.parametrize(
    "text, folded",
    [
        ("أحمد علي", "احمد علي"),  # alef with hamza
        ("إِسْلَام", "اسلام"),  # alef below, harakat
        ("آمنة", "امنه"),  # alef madda, ta marbuta
        ("مدرسة", "مدرسه"),  # ta marbuta
        ("مصطفى", "مصطفي"),  # alef maksura
        ("مؤمن", "مومن"),
        ("قائد", "قايد"),
        ("محـــمد", "محمد"),  # tatweel
        ("کتاب", "كتاب"),  # Persian kaf
        ("٢٠٢٦", "2026"),  # Arabic-Indic digits
        ("۱۲۳", "123"),  # Persian digits
        ("العملاء", "عملاء"),  # article stripped
        ("مدرسة النور", "مدرسه نور"),  # per word
        ("الى", "الي"),  # too short to strip
        ("Müller GmbH", "muller gmbh"),
        ("Straße", "strasse"),
        ("Crème Brûlée", "creme brulee"),
        ("  John   DOE ", "john doe"),
        ("ﬁle", "file"),  # NFKC ligature
        ("", ""),
        (None, ""),
        (1043, "1043"),
    ],
)
def test_fold(text, folded):
    assert fold(text) == folded


def test_article_conflation_is_symmetric():
    assert fold("الماس") == fold("ماس")
