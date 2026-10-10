"""Follow-ups, search by meaning, "ask about this document" and feedback through the HTTP API."""
import json
import time

import pytest

from fake_library import Fake
from test_features import make_client, wait_for_cards


def wait_for_index(c, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        s = c.get("/api/stats").json()["search_by_meaning"]
        if s.get("indexed") and s["pending"] == 0:
            return s
        time.sleep(0.05)
    raise AssertionError(f"embedding didn't finish: {s}")


@pytest.fixture
def fake():
    return Fake()


def ask(c, q, **kw):
    return c.post("/api/ask", json={"q": q, **kw}).json()


def test_search_by_meaning_finds_a_document_without_the_word(monkeypatch, fake, tmp_path):
    _, c = make_client(monkeypatch, fake, tmp_path, FACTS="off")
    with c:
        wait_for_index(c)
        r = ask(c, "Where is my rent agreement?")
        # still an honest "couldn't find", but the closest document in meaning comes first, without the model
        assert r["refused"] and r["mode"] == "none" and not fake.chats
        assert r["sources"][0]["id"] == 11 and r["semantic"]["11"] >= 0.9 and "closest in meaning" in r["answer"]
        r = ask(c, "Where is my rent agreement?", full=True, debug=True)  # "Ask the AI anyway"
        assert r["mode"] == "llm" and "no document mentions “rent”" in r["prompt"] and "Riverside Apartments" in r["prompt"]
    _, c = make_client(monkeypatch, fake, tmp_path, FACTS="off", EMBED_MODEL="off")
    with c:
        r = ask(c, "Where is my rent agreement?", no_llm=True)
        assert r["refused"] and r["semantic"] == {} and "closest in meaning" not in r["answer"]
        assert c.get("/api/stats").json()["search_by_meaning"] == {"enabled": False}


def test_missing_embedding_model_switches_off_cleanly(monkeypatch, tmp_path):
    fake = Fake()
    fake.embed_model_installed = False
    _, c = make_client(monkeypatch, fake, tmp_path, FACTS="off")
    with c:
        end = time.time() + 5
        while c.get("/api/stats").json()["search_by_meaning"]["available"] and time.time() < end:
            time.sleep(0.05)
        s = c.get("/api/stats").json()["search_by_meaning"]
        assert s["available"] is False and s["indexed"] == 0
        assert ask(c, "When does my car insurance expire?", no_llm=True)["sources"][0]["id"] == 2


def test_follow_ups(monkeypatch, fake, tmp_path):
    _, c = make_client(monkeypatch, fake, tmp_path)
    with c:
        wait_for_cards(c)
        first = ask(c, "When does my car insurance expire?")
        assert first["mode"] == "facts" and first["followup"] is False
        prev = {"q": first["context_q"], "a": first["answer"]}
        r = ask(c, "And the policy number?", prev=prev)
        assert r["followup"] and r["mode"] == "facts" and r["answer"].startswith("Policy Number: 123456789")
        assert r["context_q"] == "When does my car insurance expire? And the policy number?"
        # follow-ups without a fact word of their own, or asking for an older version, go to the model
        assert ask(c, "and last year's?", prev=prev)["mode"] == "llm"
        assert ask(c, "What about the other car?", prev=prev)["mode"] == "llm"
        r = ask(c, "and last year's?", prev=prev, full=True, debug=True)
        assert r["mode"] == "llm" and 3 in [s["id"] for s in r["sources"][:2]]
        assert "Earlier in this conversation the user asked: When does my car insurance expire?" in r["prompt"]
        assert "10/22/2025" in r["prompt"]  # last year's card wasn't filtered out as an old copy
        r = ask(c, "How much was the flooring install?", prev=prev)
        assert r["followup"] is False


def test_follow_up_stream_params(monkeypatch, fake, tmp_path):
    _, c = make_client(monkeypatch, fake, tmp_path, FACTS="off")
    with c:
        text = c.get("/api/ask/stream", params={"q": "and the other one?", "prev_q": "What's my auto policy number?",
                                                "prev_a": "Policy Number: 123456789 [1]."}).text
        sources = json.loads(text.split("\n\n")[0].split("data: ", 1)[1])
        assert sources["followup"] and sources["context_q"] == "What's my auto policy number? and the other one?"
        assert "Earlier in this conversation" in fake.chats[-1]["messages"][1]["content"]


def test_ask_about_one_document(monkeypatch, fake, tmp_path):
    _, c = make_client(monkeypatch, fake, tmp_path, FACTS="off")
    with c:
        r = ask(c, "Are pets allowed?", doc=11, debug=True)
        assert r["scoped"] == 11 and [s["id"] for s in r["sources"]] == [11] and r["mode"] == "llm"
        assert "Landlord allows one cat" in r["prompt"] and "[2]" not in r["prompt"]
        assert ask(c, "Are pets allowed?", doc=999, no_llm=True)["scoped"] is None  # unknown document: normal search


def test_feedback_round_trip(monkeypatch, fake, tmp_path):
    _, c = make_client(monkeypatch, fake, tmp_path, FACTS="off")
    with c:
        assert c.post("/api/feedback", json={"id": "x1", "q": "What was my DTE bill?", "vote": "sideways"}).status_code == 400
        assert c.post("/api/feedback", json={"id": "x1", "q": "What was my DTE bill?", "vote": "down", "mode": "none",
                                             "missing": ["DTE"], "sources": [{"id": 4, "title": "Utility statement"}]}).json() == {"ok": True}
        c.post("/api/feedback", json={"id": "x1", "q": "What was my DTE bill?", "vote": "down", "comment": "electric bill"})
        f = c.get("/api/feedback").json()
        assert f["down"] == 1 and f["items"][0]["comment"] == "electric bill" and f["items"][0]["missing"] == ["DTE"]
        page = c.get("/feedback").text
        assert "What was my DTE bill?" in page and "ASK_SYNONYMS=dte=otherword" in page
        assert '"feedback": true' in c.get("/").text
    assert (tmp_path / "feedback.jsonl").exists()
    _, c = make_client(monkeypatch, fake, tmp_path, FACTS="off", FEEDBACK="off")
    with c:
        assert c.post("/api/feedback", json={"id": "x", "q": "q", "vote": "up"}).status_code == 404
        assert c.get("/feedback").status_code == 404


def test_instant_answers_need_a_card_about_the_subject(monkeypatch, tmp_path):
    """A lease that mentions a passport must not answer "when does my passport expire?" with its own expiry."""
    appmod, _ = make_client(monkeypatch, Fake(), tmp_path)

    class D:
        def __init__(self, id, title):
            self.id, self.title, self.correspondent = id, title, None
    lease = {"vendor": "ACAR Leasing", "kind": "contract", "category": "vehicle", "summary": "Car lease agreement.",
             "ids": [], "dates": [{"label": "expires", "date": "2099-04-19"}]}
    monkeypatch.setattr(appmod, "card_for", lambda doc: lease)
    r = {"llm_docs": [(1, D(3, "IMG_6565"), [])], "refused": False, "listing": False}
    assert appmod.fact_answer("When does my passport expire?", r) is None
    assert appmod.fact_answer("When does my car lease expire?", r).startswith("ACAR Leasing expires on April 19, 2099")
    assert appmod.fact_answer("When does it expire?", r) is not None  # no subject words: nothing to contradict
