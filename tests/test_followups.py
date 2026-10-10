"""Follow-up detection and the combined question (no network)."""
from paperless_ask.followups import plan


def test_new_questions_stay_new():
    assert plan("When does my car insurance expire?", None, 3) == \
        {"search": "When does my car insurance expire?", "followup": False, "older": False}
    p = plan("How much was the flooring install?", "When does my car insurance expire?", 2)
    assert p["followup"] is False and p["search"] == "How much was the flooring install?"


def test_continuations_and_references_combine():
    prev = "When does my car insurance expire?"
    for q in ["and last year's?", "What about the other car?", "And the policy number?", "how about it?", "expired?"]:
        p = plan(q, prev, 1 if q != "And the policy number?" else 2)
        assert p["followup"] and p["search"] == f"{prev} {q}", q
    assert plan("and last year's?", prev, 0)["older"] is True


def test_a_new_period_replaces_the_old_one():
    p = plan("and in 2024?", "How much did I spend on medical in 2025?", 0)
    assert p["search"] == "How much did I spend on medical in ? and in 2024?"
    p = plan("what about last year?", "What did I spend in March 2026 at Apple?", 0)
    assert p["search"] == "What did I spend in at Apple? what about last year?" and p["older"]
