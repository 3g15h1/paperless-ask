"""Fact cards, instant answers, totals and reminders through the HTTP API (fake Paperless + Ollama)."""
import importlib
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import fake_library
from fake_library import SOON, Fake


def make_client(monkeypatch, fake, tmp_path, **env):
    env = {"FACTS": "on", "DATA_DIR": str(tmp_path), "CHECK_SECONDS": "0", **env}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import paperless_ask.app as appmod
    appmod = importlib.reload(appmod)
    monkeypatch.setattr(appmod, "HTTP_TRANSPORT", httpx.MockTransport(fake))
    appmod.STATE.update(facts_poll=0.01, facts_idle=0)
    return appmod, TestClient(appmod.app)


def wait_for_cards(c, pending=0, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        st = c.get("/api/facts").json()
        if st["analysed"] and st["pending"] == pending:
            return st
        time.sleep(0.05)
    raise AssertionError(f"extraction didn't finish: {st}")


@pytest.fixture
def fake():
    return Fake()


def test_worker_extracts_grounds_and_persists(monkeypatch, fake, tmp_path):
    _, c = make_client(monkeypatch, fake, tmp_path)
    with c:
        st = wait_for_cards(c)
        cards = {x["id"]: x for x in st["cards"]}
        assert st["analysed"] == len(fake_library.DOCS) - 1  # doc 10 has no text
        assert cards[2]["ids"] == [{"label": "Policy Number", "value": "123456789"}]
        assert "id member='JOHN DOE'" in cards[2]["dropped"] and cards[2]["total"] is None
        assert [d["date"] for d in cards[4]["dates"] if d["label"] == "due"] == ["2025-09-05"]
        assert cards[9]["vendor"] == ""
        n_text = len([d for d in fake_library.DOCS if d["content"]])
        assert c.get("/healthz").json()["facts"] == {"enabled": True, "analysed": n_text, "pending": 0, "hours": "any"}
    assert (tmp_path / "facts.json").exists()
    n = len(fake.extractions)
    _, c = make_client(monkeypatch, fake, tmp_path)  # restart: cards come from disk
    with c:
        assert c.get("/api/facts").json()["analysed"] == len([d for d in fake_library.DOCS if d["content"]])
        time.sleep(0.3)
    assert len(fake.extractions) == n


def test_changed_document_is_reextracted(monkeypatch, fake, tmp_path):
    _, c = make_client(monkeypatch, fake, tmp_path)
    with c:
        wait_for_cards(c)
        n = len(fake.extractions)
        monkeypatch.setitem(fake_library.DOCS[0], "modified", "2030-01-01T00:00:00Z")
        c.get("/api/stats")  # notices the change
        wait_for_cards(c)
        assert len(fake.extractions) == n + 1


def test_instant_answers_from_cards(monkeypatch, fake, tmp_path):
    _, c = make_client(monkeypatch, fake, tmp_path)
    with c:
        wait_for_cards(c)
        ask = lambda q, **kw: c.post("/api/ask", json={"q": q, **kw}).json()
        r = ask("When does my car insurance expire?")
        assert (r["mode"], r["answer"]) == ("facts", "Example Mutual insurance expires on October 22, 2026 [1].")
        r = ask("What's my policy number?")
        assert (r["mode"], r["answer"]) == ("facts", "Policy Number: 123456789 [1] (Example Mutual).")
        r = ask("When is my Northwind payment due?")
        assert r["mode"] == "facts"
        assert r["answer"] == f"$250.00 is due to Northwind Card Services on {SOON:%B} {SOON.day}, {SOON.year} [1]."
        assert not fake.chats
        assert ask("When does my car insurance expire?", full=True)["mode"] == "llm" and fake.chats
        assert ask("What did I buy at the gadget store?")["mode"] == "llm"  # not a single-fact question


def test_totals(monkeypatch, fake, tmp_path):
    _, c = make_client(monkeypatch, fake, tmp_path)
    with c:
        wait_for_cards(c)
        r = c.post("/api/ask", json={"q": "How much did I spend on medical in 2025?"}).json()
        assert r["mode"] == "total" and not fake.chats and r["total"]["pending"] == 0  # doc 10 can't be analysed
        assert r["answer"] == "Total on medical in 2025: $100.65 across 1 document [1]."
        assert r["total"]["total"] == 100.65 and [s["id"] for s in r["sources"]] == [5]
        assert [x["id"] for x in r["total"]["excluded"]] == [6]
        events = [b.split("\n")[0] for b in c.get("/api/ask/stream", params={"q": "Total spent on medical"}).text.split("\n\n") if b]
        assert events == ["event: sources", "event: done"]


def test_upcoming_and_calendar(monkeypatch, fake, tmp_path):
    _, c = make_client(monkeypatch, fake, tmp_path, ALERT_DAYS="10")
    with c:
        wait_for_cards(c)
        up = c.get("/api/upcoming").json()
        ids = {(i["id"], i["label"]) for i in up["items"]}
        assert (8, "due") in ids and not any(i == 9 for i, _ in ids)  # doc 9 is tagged Paid
        item = next(i for i in up["items"] if i["id"] == 8)
        assert item["amount"] == 250.0 and item["days_left"] == 10 and item["url"].endswith("/documents/8/details")
        assert [i["id"] for i in up["alerts"]] == [8]
        cal = c.get("/calendar.ics")
        assert cal.headers["content-type"].startswith("text/calendar")
        assert f"UID:8-due-{SOON:%Y%m%d}@testserver" in cal.text
        cfg = c.get("/").text
        assert '"facts": true' in cfg and '"reminder_days": 60' in cfg


def test_facts_off_keeps_pattern_reminders(monkeypatch, fake, tmp_path):
    _, c = make_client(monkeypatch, fake, tmp_path, FACTS="off")
    with c:
        time.sleep(0.2)
        up = c.get("/api/upcoming").json()
        assert any(i["id"] == 8 and i["label"] == "due" for i in up["items"])
        r = c.post("/api/ask", json={"q": "How much did I spend on medical in 2025?", "no_llm": True}).json()
        assert r["mode"] != "total"
    assert not fake.extractions and not (tmp_path / "facts.json").exists()


def test_date_answers_skip_past_dates(monkeypatch, fake, tmp_path):
    """Last decade's insurance cards also have expiry dates; only the one still ahead answers 'when does it expire'."""
    from datetime import date, timedelta
    appmod, _ = make_client(monkeypatch, fake, tmp_path)

    class D:
        def __init__(self, id):
            self.id, self.title, self.correspondent = id, f"Card {id}", None
    future = (date.today() + timedelta(days=30)).isoformat()
    cards = {1: {"vendor": "Example Mutual", "kind": "insurance", "dates": [{"label": "expires", "date": "2014-04-22"}]},
             2: {"vendor": "Example Mutual", "kind": "insurance", "dates": [{"label": "expires", "date": future}]},
             3: {"vendor": "Other Co", "kind": "insurance", "dates": [{"label": "expires", "date": "2018-10-22"}]}}
    monkeypatch.setattr(appmod, "card_for", lambda doc: cards[doc.id])
    r = {"llm_docs": [(1, D(1), []), (2, D(2), []), (3, D(3), [])], "refused": False, "listing": False}
    d = date.fromisoformat(future)
    assert appmod.fact_answer("When does my car insurance expire?", r) == \
        f"Example Mutual insurance expires on {d:%B} {d.day}, {d.year} [2]."
    cards[1]["dates"][0]["date"] = future[:4] + "-12-31" if future[5:] != "12-31" else future[:4] + "-12-30"
    assert appmod.fact_answer("When does my car insurance expire?", r) is None  # two future dates: let the model decide
