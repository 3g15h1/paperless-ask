"""Answer verification and citation tidying (no network)."""
import pytest

from paperless_ask.app import Doc, _tidy_citations, verify_answer

NAMES = {"correspondents": {}, "document_types": {}, "tags": {}}
D1 = Doc({"id": 1, "title": "Receipt", "created": "2024-10-03",
          "content": "Gadget Store Main Street October 03, 2024 07:45 PM Phone X Pro $ 1,099.00"}, NAMES)
D2 = Doc({"id": 2, "title": "Card", "created": "2026-09-26",
          "content": "Policy Number: 123456789 Effective Date: 04/22/2026 Expiration Date: 10/22/2026"}, NAMES)
DOCS = [(1, D1, []), (2, D2, [])]


@pytest.mark.parametrize("text, unverified, expect", [
    ("The Phone X Pro cost $1,099.00 [1].", [], "The Phone X Pro cost $1,099.00 [1]."),
    ("It cost $1,099 on October 3, 2024.", [], "It cost $1,099 on October 3, 2024 [1]."),
    ("[2] Your car insurance expires on October 22, 2026.", [], "Your car insurance expires on October 22, 2026 [2]."),
    ("Your policy number is 123456789.", [], "Your policy number is 123456789 [2]."),
    ("Your wages were $98,765.43 [1].", ["98,765.43"], None),
    ("It expires 11/30/2026 [2].", ["11/30/2026"], None),
    ("You bought a phone at the store. [1].", [], "You bought a phone at the store [1]."),
    ("It expires on October 22, 2026 [2] [2].", [], "It expires on October 22, 2026 [2]."),
    ("It expires on October 22, 2026 [2]. [2]", [], "It expires on October 22, 2026 [2]."),
    ("It expires on October 22, 2026. [2] [2]", [], "It expires on October 22, 2026 [2]."),
    ("You bought earbuds on September 11, 2022, [1].", ["September 11, 2022"], None),
])
def test_verify_answer(text, unverified, expect):
    v = verify_answer(text, DOCS)
    assert v["unverified"] == unverified
    if expect is not None:
        assert v["answer"] == expect


def test_tidy_moves_leading_citations():
    assert _tidy_citations("[1] $100.65 for A on 6/6/2022. [2] $32.26 for B on 10/29/2023.") == \
        "$100.65 for A on 6/6/2022 [1]. $32.26 for B on 10/29/2023 [2]."
