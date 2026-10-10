"""Feedback log: validation, size caps, comment updates, review page."""
import pytest

from paperless_ask.feedback import FeedbackLog, clean, render


def test_clean_validates_and_caps():
    with pytest.raises(ValueError):
        clean({"id": "a", "q": "x", "vote": "meh"})
    with pytest.raises(ValueError):
        clean({"vote": "up", "q": "x"})
    e = clean({"id": "a", "q": "x" * 900, "vote": "down", "sources": [{"id": 3, "title": "T"}, {"id": "evil"}, "x"],
               "missing": ["DTE"], "doc": 5, "extra": "ignored"})
    assert len(e["q"]) == 500 and e["sources"] == [{"id": 3, "title": "T"}] and e["doc"] == 5 and "extra" not in e


def test_log_merges_comment_and_renders(tmp_path):
    log = FeedbackLog(tmp_path / "f.jsonl")
    log.add(clean({"id": "a", "q": "What was my DTE bill?", "vote": "down", "missing": ["DTE"], "answer": "<b>nope</b>"}))
    log.add(clean({"id": "b", "q": "Policy?", "vote": "up"}))
    log.add(clean({"id": "a", "q": "What was my DTE bill?", "vote": "down", "comment": "It's on the electric statement"}))
    (tmp_path / "f.jsonl").open("a").write("not json\n")
    es = log.entries()
    assert len(es) == 2 and log.summary() == {"up": 1, "down": 1, "total": 2}
    a = next(e for e in es if e["id"] == "a")
    assert a["comment"] == "It's on the electric statement" and a["missing"] == ["DTE"]
    page = render(es, lambda i: f"/d/{i}")
    assert "ASK_SYNONYMS=dte=otherword" in page and "&lt;b&gt;nope&lt;/b&gt;" in page
    assert page.index("DTE bill") < page.index("Policy?")  # wrong answers first
