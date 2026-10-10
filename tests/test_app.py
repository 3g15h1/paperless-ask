"""End-to-end through the HTTP API against a fake Paperless and Ollama."""
import base64
import importlib
import json

import pytest
from fastapi.testclient import TestClient

from fake_library import Fake


def make_client(monkeypatch, fake, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import paperless_ask.app as appmod
    appmod = importlib.reload(appmod)
    import httpx
    monkeypatch.setattr(appmod, "HTTP_TRANSPORT", httpx.MockTransport(fake))
    return appmod, TestClient(appmod.app)


@pytest.fixture
def fake():
    return Fake()


def sse_events(text):
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_health_and_page(monkeypatch, fake):
    _, c = make_client(monkeypatch, fake)
    with c:
        h = c.get("/healthz").json()
        assert h["ok"] and h["paperless"]["documents"] == 4 and h["llm"]["ok"]
        page = c.get("/").text
        assert "window.PAPERLESS_ASK" in page and "<!--CONFIG-->" not in page
        assert c.get("/manifest.webmanifest").json()["name"] == "Paperless Ask"
        assert c.get("/icon.png").content.startswith(b"\x89PNG")
        assert c.get("/thumb/1").status_code == 200
        assert c.get("/thumb/999").status_code == 404


def test_llm_answer_is_verified_and_cited(monkeypatch, fake):
    _, c = make_client(monkeypatch, fake)
    with c:
        r = c.post("/api/ask", json={"q": "When does my car insurance expire?"}).json()
    assert r["mode"] == "llm"
    assert r["answer"] == "Your car insurance expires on October 22, 2026 [1]."
    assert r["unverified"] == []
    # newest card first; last year's near-copy is kept out of the model's prompt
    assert r["sources"][0]["id"] == 2
    prompt = fake.chats[0]["messages"][1]["content"]
    assert "10/22/2026" in prompt and "10/22/2025" not in prompt
    assert "num_thread" not in fake.chats[0]["options"]


def test_unverified_number_is_flagged(monkeypatch):
    fake = Fake(answer="It expires on 11/30/2027 [1].")
    _, c = make_client(monkeypatch, fake)
    with c:
        r = c.post("/api/ask", json={"q": "When does my car insurance expire?"}).json()
    assert r["unverified"] == ["11/30/2027"]


def test_missing_term_refuses_without_llm(monkeypatch, fake):
    _, c = make_client(monkeypatch, fake)
    with c:
        r = c.post("/api/ask", json={"q": "What was my Zorblax bill?"}).json()
    assert r["refused"] and r["missing"] == ["Zorblax"] and not fake.chats
    assert "Zorblax" in r["answer"]


def test_synonyms_from_env(monkeypatch, fake):
    # "hydro" appears in no document; ASK_SYNONYMS=hydro=acmepower (conftest) maps it to the utility
    _, c = make_client(monkeypatch, fake)
    with c:
        r = c.post("/api/ask", json={"q": "How much was my hydro bill?", "no_llm": True}).json()
        assert not r["refused"] and r["sources"][0]["id"] == 4
        assert {"kind": "amount", "value": "$ 142.37", "n": 1} in r["extractive"]["facts"]
    _, c = make_client(monkeypatch, fake, ASK_SYNONYMS="")
    with c:
        r = c.post("/api/ask", json={"q": "How much was my hydro bill?", "no_llm": True}).json()
        assert r["refused"] and r["missing"] == ["hydro"]


def test_ollama_down_falls_back_to_extractive(monkeypatch):
    fake = Fake(ollama_up=False)
    _, c = make_client(monkeypatch, fake)
    with c:
        events = sse_events(c.get("/api/ask/stream", params={"q": "What did my phone cost?"}).text)
        stats = c.get("/api/stats").json()
    kinds = [e for e, _ in events]
    assert kinds[0] == "sources" and "llm_error" in kinds and events[-1] == ("done", events[-1][1])
    assert events[-1][1]["mode"] == "extractive"
    assert events[0][1]["sources"][0]["id"] == 1
    assert stats["llm"] is False


def test_stream_tokens(monkeypatch, fake):
    _, c = make_client(monkeypatch, fake)
    with c:
        events = sse_events(c.get("/api/ask/stream", params={"q": "When does my car insurance expire?"}).text)
    assert [e for e, _ in events][:2] == ["sources", "stage"]
    assert "".join(d["t"] for e, d in events if e == "token").startswith("Your car insurance")
    assert events[-1][1]["mode"] == "llm"


def test_list_question_needs_no_llm(monkeypatch, fake):
    _, c = make_client(monkeypatch, fake)
    with c:
        r = c.post("/api/ask", json={"q": "Show me my insurance cards"}).json()
    assert r["mode"] == "list" and not fake.chats


def test_basic_auth(monkeypatch, fake):
    _, c = make_client(monkeypatch, fake, ASK_PASSWORD="s3cret")
    good = "Basic " + base64.b64encode(b"ask:s3cret").decode()
    bad = "Basic " + base64.b64encode(b"ask:nope").decode()
    with c:
        assert c.get("/").status_code == 401
        assert c.get("/", headers={"Authorization": bad}).status_code == 401
        assert c.get("/", headers={"Authorization": good}).status_code == 200
        assert c.post("/api/ask", json={"q": "x"}).status_code == 401
        h = c.get("/healthz")
        assert h.status_code == 200 and "url" not in h.json()["paperless"]


def test_suggestions_and_threads_from_env(monkeypatch, fake):
    _, c = make_client(monkeypatch, fake, ASK_SUGGESTIONS="🚗 When is my registration due?|Find my lease",
                       OLLAMA_THREADS="2")
    with c:
        assert '"suggestions": ["\\ud83d\\ude97 When is my registration due?", "Find my lease"]' in c.get("/").text
        c.post("/api/ask", json={"q": "When does my car insurance expire?"})
    assert fake.chats[0]["options"]["num_thread"] == 2


def test_missing_token_fails_clearly(monkeypatch, fake):
    monkeypatch.setenv("PAPERLESS_TOKEN", "")
    _, c = make_client(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="PAPERLESS_TOKEN"):
        with c:
            pass
