"""Paperless Ask: natural-language Q&A over Paperless-ngx, answered by a local Ollama model.

Pipeline per question:
  1. keep an in-memory copy of every document's OCR text (refreshed when Paperless changes)
  2. rank documents with BM25 + date/intent boosts, fused with Paperless's own full-text search
  3. pick the most relevant passages from the top documents
  4. stream a short cited answer from a local Ollama model; if that fails, the
     extractive passages are the answer (the app stays useful without the LLM)

All configuration is via environment variables; see .env.example.
"""
import asyncio
import base64
import difflib
import json
import logging
import math
import os
import re
import secrets
import struct
import time
import zlib
from collections import Counter, OrderedDict
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from . import __version__

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("paperless-ask")


def _env_int(name: str, default):
    v = os.environ.get(name, "").strip()
    return int(v) if v else default


PAPERLESS_URL = os.environ.get("PAPERLESS_URL", "http://localhost:8000").rstrip("/")
# Where the browser should open documents, if different from the URL the app uses (e.g. a public hostname)
PAPERLESS_LINK_URL = (os.environ.get("PAPERLESS_LINK_URL") or PAPERLESS_URL).rstrip("/")
PAPERLESS_TOKEN = os.environ.get("PAPERLESS_TOKEN", "").strip()
TOKEN_FILE = os.environ.get("PAPERLESS_TOKEN_FILE", "").strip()
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b-instruct")
# Unset = let Ollama choose. On CPU-limited containers set this to the number of cores it may use:
# Ollama sizes threads from the host's cores, and oversubscribing can make generation ~50x slower.
OLLAMA_THREADS = _env_int("OLLAMA_THREADS", None)
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "24h")
OLLAMA_WARM = os.environ.get("OLLAMA_WARM", "true").lower() not in ("0", "false", "no")
REFRESH_SECONDS = _env_int("REFRESH_SECONDS", 300)
CHECK_SECONDS = _env_int("CHECK_SECONDS", 30)
LLM_DOCS = _env_int("LLM_DOCS", 3)
MAX_SOURCES = _env_int("MAX_SOURCES", 5)
# Optional HTTP Basic auth for the whole app (except /healthz). Unset = no login.
ASK_USERNAME = os.environ.get("ASK_USERNAME", "ask")
ASK_PASSWORD = os.environ.get("ASK_PASSWORD", "")
# Optional "|"-separated example questions shown as chips; each may start with an emoji and a space.
SUGGESTIONS = [s.strip() for s in os.environ.get("ASK_SUGGESTIONS", "").split("|") if s.strip()]
STATIC_DIR = Path(__file__).with_name("static")

# ---------------------------------------------------------------- text utils

STOP = set("""
a about above after again against all also am an and any are as at be been before being below
between both but by can could did do does doing done during each few for from further get got had
has have having he her here hers him his how i if in into is it its itself just me more most my
myself no nor not now of off on once only or other our ours out over own same she should so some
such than that the their them then there these they this those through to too under until up very
was we were what whats when where which while who whom why will with would you your yours
yourself show find tell give list see look please much many anything something thing things
document documents doc docs paperless file files filed paper papers ive im what's i'm
""".split())

# Query word -> related words worth searching for too (weighted lower than the word itself).
# Extra entries can be added without code changes via ASK_SYNONYMS (see .env.example).
EXPANSIONS = {
    "car": ["auto", "vehicle"], "auto": ["vehicle"], "vehicle": ["auto"],
    "insurance": ["policy", "insurer"], "insurer": ["insurance"],
    "w2": ["wage", "w-2"], "salary": ["wage", "compensation", "w2"], "wage": ["w2", "compensation"],
    "income": ["wage", "w2", "earning"], "earning": ["wage", "w2"], "pay": ["wage", "paid"],
    "paycheck": ["wage", "earning"], "phone": ["iphone", "mobile", "wireless"], "mobile": ["phone", "wireless"],
    "hotel": ["room", "inn", "resort"], "doctor": ["patient", "medical", "clinic"],
    "medical": ["patient", "health", "clinic"], "hospital": ["patient", "health"],
    "electric": ["energy", "electricity", "utility"], "electricity": ["electric", "energy", "utility"],
    "power": ["electric", "energy"], "gas": ["energy", "utility"], "water": ["sewer", "utility"],
    "utility": ["electric", "gas", "water"],
    "lawn": ["mowing", "property"], "landscaping": ["lawn"], "tax": ["irs"], "taxe": ["tax", "irs"],
    "irs": ["tax"], "registration": ["title", "plate"], "plate": ["registration"],
    "internet": ["broadband", "wifi"], "cable": ["tv", "broadband"], "floor": ["flooring"],
    "flooring": ["floor", "installation"],
    "headphone": ["earbud", "airpod"], "earbud": ["headphone", "airpod"], "airpod": ["headphone"],
    "bill": ["invoice", "statement", "due"], "invoice": ["bill"], "receipt": ["purchase"],
    "lawyer": ["attorney", "law"], "attorney": ["law"],
}


def _load_synonyms(spec: str) -> None:
    """ASK_SYNONYMS="electric=acme,acmeenergy; internet=fastnet" adds to EXPANSIONS (single words)."""
    for entry in spec.split(";"):
        if "=" not in entry:
            continue
        word, related = entry.split("=", 1)
        word = stem(word.strip().lower())
        extra = [w.strip().lower() for w in related.split(",") if w.strip()]
        if word and extra:
            EXPANSIONS[word] = list(dict.fromkeys(EXPANSIONS.get(word, []) + extra))


MONTH_NAMES = ["", "january", "february", "march", "april", "may", "june", "july", "august",
               "september", "october", "november", "december"]
MONTHS = {}
for _i, _name in enumerate(MONTH_NAMES[1:], 1):
    MONTHS[_name] = _i
    MONTHS[_name[:3]] = _i
MONTHS["sept"] = 9

TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-'.@][a-z0-9]+)*")
MONEY_RE = re.compile(r"\$\s?\(?\d[\d,]*(?:\.\d{2})?\)?|(?<![\d.,/-])\d{1,3}(?:,?\d{3})*\.\d{2}(?![\d.])")
DATE_RE = re.compile(
    r"\b(?:\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? \d{1,2},? \d{4})\b", re.I)
AMOUNT_Q = re.compile(r"\b(how much|cost|costs|price|paid|pay|total|amount|owe|spent|spend|charged?|"
                      r"bill|fee|wages?|salary|income|earn(ed|ings)?)\b", re.I)
DATE_Q = re.compile(r"\b(when|date|what day|expire[sd]?|expiration|due|valid)\b", re.I)
NUMBER_Q = re.compile(r"\b(number|no\.?|policy|account|serial|id|vin|plate|imei|reference|confirmation)\b", re.I)
RECENT_Q = re.compile(r"\b(latest|recent|newest|last|most recent|current|currently|now|expire[sd]?|expiration|"
                      r"valid|renew(al)?)\b", re.I)
NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
ID_RE = re.compile(r"(?:number|no\.|#)\s*:?\s*([A-Z0-9][A-Z0-9-]{4,})\b", re.I)
LIST_Q = re.compile(r"^\s*(show|list|find|pull up|open|get)( me)?\b|\ball (of )?my\b|\bwhich documents\b", re.I)


def stem(w: str) -> str:
    if w.endswith("'s"):
        w = w[:-2]
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("sses", "xes", "ches", "shes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


_load_synonyms(os.environ.get("ASK_SYNONYMS", ""))


# Question words that describe *what kind* of answer is wanted rather than *which* document.
# A query term outside this list that appears in no document at all means "not in the library".
GENERIC = {stem(w) for w in """
cost costs price prices paid pay paying spend spent buy bought purchase purchased total amount amounts
owe owed due charge charged fee fees number numbers date dates day days year years month months
week weeks time times bill bills invoice invoices receipt receipts statement statements expire expires
expired expiration last latest recent newest first info information detail details summary copy
mine record records get got send sent come came recently ago old new current renew renewal
""".split()}


def _words(text: str):
    for w in TOKEN_RE.findall(text.lower().replace("’", "'")):
        w = w.strip(".'")
        if w:
            yield w


def tokenize(text: str) -> list[str]:
    out = []
    for w in _words(text):
        s = stem(w)
        out.append(s)
        if "-" in s:
            out.append(s.replace("-", ""))
    return out


def query_terms(question: str) -> tuple[list[str], list[str]]:
    """Content terms (stemmed, stopwords removed) and the raw words to highlight."""
    terms, raw = [], []
    for w in _words(question):
        if w in STOP or (len(w) < 2 and not w.isdigit()):
            continue
        s = stem(w)
        if s in STOP:
            continue
        terms.append(s)
        raw.append(w)
        if "-" in s:
            terms.append(s.replace("-", ""))
    return terms, raw


def wants_month(question: str) -> list[int]:
    q = question.lower()
    found = set()
    for w in re.findall(r"[a-z]+", q):
        if w in MONTHS and w not in ("may", "mar"):
            found.add(MONTHS[w])
    if re.search(r"\b(in|of|during|since) may\b|\bmay \d{4}\b", q):
        found.add(5)
    if re.search(r"\b(in|of|during|since) mar\b", q):
        found.add(3)
    return sorted(found)


def wants_year(question: str) -> list[int]:
    return sorted({int(y) for y in re.findall(r"\b(19[5-9]\d|20[0-4]\d)\b", question)})


# ---------------------------------------------------------------- library cache

def split_pieces(content: str, size: int = 220) -> list[str]:
    pieces = []
    for line in content.splitlines():
        line = " ".join(line.split())
        while len(line) > size:
            cut = line.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            pieces.append(line[:cut])
            line = line[cut:].strip()
        if line:
            pieces.append(line)
    return pieces


def make_headline(content: str) -> str:
    for line in content.splitlines():
        line = " ".join(line.split())
        letters = sum(c.isalpha() for c in line)
        if len(line) >= 12 and letters / len(line) > 0.6 and not line.lower().startswith("form_") and "<" not in line:
            return line[:90]
    return " ".join(content.split())[:90]


class Doc:
    __slots__ = ("id", "title", "created", "content", "correspondent", "document_type", "tags",
                 "tf", "length", "pieces", "headline", "lower")

    def __init__(self, raw: dict, names: dict):
        self.id = raw["id"]
        self.title = raw.get("title") or f"Document {self.id}"
        self.created = (raw.get("created") or "")[:10]
        self.content = raw.get("content") or ""
        self.correspondent = names["correspondents"].get(raw.get("correspondent"))
        self.document_type = names["document_types"].get(raw.get("document_type"))
        self.tags = [names["tags"][t] for t in raw.get("tags") or [] if t in names["tags"]]
        self.lower = self.content.lower()
        meta = " ".join(filter(None, [self.title.replace("_", " "), self.correspondent or "",
                                      self.document_type or "", " ".join(self.tags)]))
        toks = tokenize(self.content) + tokenize(meta) * 3
        self.tf = Counter(toks)
        self.length = max(len(toks), 1)
        self.pieces = split_pieces(self.content)
        self.headline = make_headline(self.content)

    @property
    def created_date(self):
        try:
            return date.fromisoformat(self.created)
        except ValueError:
            return None

    def card(self, n: int, snippet: str, score: float) -> dict:
        return {
            "n": n, "id": self.id, "title": self.title, "headline": self.headline,
            "created": self.created, "type": self.document_type, "correspondent": self.correspondent,
            "tags": self.tags, "snippet": snippet, "score": round(score, 3),
            "url": f"{PAPERLESS_LINK_URL}/documents/{self.id}/details",
            "thumb": f"/thumb/{self.id}",
        }


class Library:
    def __init__(self):
        self.docs: dict[int, Doc] = {}
        self.df: Counter = Counter()
        self.avglen = 1.0
        self.vocab: list[str] = []
        self.refreshed_at = 0.0
        self.checked_at = 0.0
        self.signature = None
        self.lock = asyncio.Lock()
        self.error = None

    async def _get_all(self, client: httpx.AsyncClient, path: str, params: dict) -> list[dict]:
        out, url, p = [], f"{PAPERLESS_URL}{path}", dict(params)
        while url:
            r = await client.get(url, params=p, timeout=30)
            r.raise_for_status()
            data = r.json()
            out.extend(data["results"])
            url, p = data.get("next"), None
        return out

    async def _signature(self, client):
        r = await client.get(f"{PAPERLESS_URL}/api/documents/", timeout=10,
                             params={"page_size": 1, "ordering": "-modified", "fields": "id,modified"})
        r.raise_for_status()
        data = r.json()
        top = data["results"][0]["modified"] if data["results"] else None
        return (data["count"], top)

    async def refresh(self, client, force=False):
        async with self.lock:
            sig = await self._signature(client)
            self.checked_at = time.time()
            if not force and sig == self.signature and self.docs:
                return False
            t0 = time.monotonic()
            names = {}
            for key in ("correspondents", "document_types", "tags"):
                rows = await self._get_all(client, f"/api/{key}/", {"page_size": 500, "fields": "id,name"})
                names[key] = {o["id"]: o["name"] for o in rows}
            raw = await self._get_all(client, "/api/documents/", {
                "page_size": 100,
                "fields": "id,title,created,content,correspondent,document_type,tags,modified"})
            docs = {d["id"]: Doc(d, names) for d in raw}
            df = Counter()
            for d in docs.values():
                df.update(d.tf.keys())
            self.docs, self.df = docs, df
            self.avglen = sum(d.length for d in docs.values()) / max(len(docs), 1)
            self.vocab = sorted(df)
            self.signature = sig
            self.refreshed_at = time.time()
            self.error = None
            log.info("library refreshed: %d documents, %d terms in %.2fs",
                     len(docs), len(df), time.monotonic() - t0)
            return True

    async def ensure_fresh(self, client):
        if time.time() - self.checked_at < CHECK_SECONDS and self.docs:
            return
        try:
            await self.refresh(client)
        except Exception as e:  # keep serving the cached copy
            self.error = f"{type(e).__name__}: {e}"
            self.checked_at = time.time()
            log.warning("library refresh failed: %s", self.error)
            if not self.docs:
                raise


LIB = Library()

# ---------------------------------------------------------------- retrieval

def idf(t: str) -> float:
    n, df = len(LIB.docs), LIB.df.get(t, 0)
    return math.log(1 + (n - df + 0.5) / (df + 0.5)) if df else 0.0


def expand_terms(terms: list[str]) -> tuple[dict[str, float], list[str]]:
    """Weighted search terms (with synonyms, prefixes, typo matches) and the terms nothing matched."""
    weights: dict[str, float] = {}
    unmatched = []

    def add(t, w):
        if weights.get(t, 0) < w:
            weights[t] = w

    for t in terms:
        found = t in LIB.df
        add(t, 1.0)
        for e in EXPANSIONS.get(t, []):
            add(stem(e), 0.45)
            found = found or stem(e) in LIB.df
        if t not in LIB.df and len(t) >= 4:
            pref = [v for v in LIB.vocab if v.startswith(t) and v != t][:3]
            for v in pref:
                add(v, 0.7)
            if not pref:
                pref = difflib.get_close_matches(t, LIB.vocab, n=2, cutoff=0.84)
                for v in pref:
                    add(v, 0.55)
            found = found or bool(pref)
        if not found and t not in GENERIC and not t.isdigit():
            unmatched.append(t)
    return weights, unmatched


def bm25(doc: Doc, weights: dict[str, float], k1=1.2, b=0.75) -> float:
    s = 0.0
    for t, w in weights.items():
        tf = doc.tf.get(t)
        if tf:
            s += w * idf(t) * tf * (k1 + 1) / (tf + k1 * (1 - b + b * doc.length / LIB.avglen))
    return s


def month_hit(doc: Doc, m: int) -> bool:
    cd = doc.created_date
    if cd and cd.month == m:
        return True
    if re.search(rf"\b{MONTH_NAMES[m][:3]}[a-z]*\.? \d{{1,2}}\b", doc.lower):
        return True
    return re.search(rf"\b0?{m}/\d{{1,2}}/\d{{2,4}}\b", doc.content) is not None


def year_hit(doc: Doc, y: int) -> bool:
    cd = doc.created_date
    return bool(cd and cd.year == y) or str(y) in doc.content


def best_windows(doc: Doc, weights: dict[str, float], question: str, k: int = 2, width: int = 360):
    """Up to k non-overlapping passages of the document most relevant to the question."""
    pieces = doc.pieces
    if not pieces:
        return []
    want_amount = bool(AMOUNT_Q.search(question))
    want_date = bool(DATE_Q.search(question))
    want_number = bool(NUMBER_Q.search(question))
    w_idf = {t: w * idf(t) for t, w in weights.items() if LIB.df.get(t)}
    cands = []
    for i in range(len(pieces)):
        text, j = pieces[i], i + 1
        while j < len(pieces) and len(text) + len(pieces[j]) < width:
            text += " " + pieces[j]
            j += 1
        toks = set(tokenize(text))
        s = sum(v for t, v in w_idf.items() if t in toks)
        if want_amount:
            s += 0.6 * min(len(MONEY_RE.findall(text)), 3)
        if want_date and DATE_RE.search(text):
            s += 0.6
        if want_number and re.search(r"\d{5,}", text):
            s += 0.6
        s -= i * 0.002  # gentle preference for earlier text
        cands.append((s, i, j, text))
    cands.sort(key=lambda c: -c[0])
    chosen = []
    for s, i, j, text in cands:
        if s <= 0 and chosen:
            break
        if all(j <= ci or i >= cj for _, ci, cj, _ in chosen):
            chosen.append((s, i, j, text))
        if len(chosen) >= k:
            break
    chosen.sort(key=lambda c: c[1])
    return [c[3] for c in chosen]


async def paperless_fulltext(client, terms: list[str]) -> list[int]:
    """Paperless's own full-text index as a second opinion (fused below)."""
    if not terms:
        return []
    try:
        r = await client.get(f"{PAPERLESS_URL}/api/documents/", timeout=4, params={
            "query": " OR ".join(dict.fromkeys(terms)), "page_size": 10, "fields": "id"})
        r.raise_for_status()
        return [d["id"] for d in r.json()["results"]]
    except Exception as e:
        log.info("paperless full-text search unavailable: %s", e)
        return []


async def retrieve(client, question: str) -> dict:
    t0 = time.monotonic()
    terms, raw = query_terms(question)
    months, years = wants_month(question), wants_year(question)
    date_words = {str(y) for y in years}
    for m in months:
        date_words |= {MONTH_NAMES[m], MONTH_NAMES[m][:3]}
    content_terms = [t for t in terms if t not in date_words and t not in MONTHS]
    weights, unmatched = expand_terms(content_terms)
    matched_specific = {t for t in content_terms if t not in unmatched and t not in GENERIC}
    missing = []
    for w in re.findall(r"[\w'’-]+", question):
        if stem(w.lower().strip("'’")) in unmatched and w not in missing:
            missing.append(w)
    refuse = bool(unmatched) and len(matched_specific) <= len(unmatched)
    for t in terms:
        if t in date_words:
            weights.setdefault(t, 0.5)
    pl_ids = await paperless_fulltext(client, content_terms)

    scores = {}
    for doc in LIB.docs.values():
        s = bm25(doc, weights)
        if not content_terms and (months or years):
            s = max(s, 1.0)
        if s <= 0:
            continue
        if months:
            s *= 1.6 if any(month_hit(doc, m) for m in months) else 0.55
        if years:
            s *= 1.6 if any(year_hit(doc, y) for y in years) else 0.45
        scores[doc.id] = s
    top = max(scores.values(), default=0)
    final = {i: s / top for i, s in scores.items()} if top else {}
    for rank, i in enumerate(pl_ids):
        if i in LIB.docs:
            final[i] = final.get(i, 0) + 0.3 / (1 + rank)
    if RECENT_Q.search(question):
        for i in final:
            cd = LIB.docs[i].created_date
            if cd:
                final[i] += 0.15 * max(0, 1 - (date.today() - cd).days / 3650)
    ranked = sorted(final.items(), key=lambda kv: -kv[1])
    best = ranked[0][1] if ranked else 0
    ranked = [(i, s) for i, s in ranked if s >= max(0.3 * best, 0.12)][:MAX_SOURCES]

    sources, llm_docs, seen = [], [], set()
    recent = bool(RECENT_Q.search(question))
    for n, (i, s) in enumerate(ranked, 1):
        doc = LIB.docs[i]
        wins = best_windows(doc, weights, question, k=3 if n == 1 else 1, width=380 if n == 1 else 320)
        sources.append(doc.card(n, wins[0] if wins else doc.headline, s))
        key = (doc.title, doc.created)
        if len(llm_docs) < LLM_DOCS and key not in seen and (not llm_docs or s >= 0.45 * best):
            # "current"-type questions: an older near-copy (last year's insurance card) only confuses the model
            if recent and any(_similar(doc, d) and (d.created or "") >= (doc.created or "") for _, d, _ in llm_docs):
                continue
            seen.add(key)
            llm_docs.append((n, doc, wins))
    listing = bool(LIST_Q.search(question)) and len(sources) > 0

    highlight = {w for w in raw if len(w) > 2} | {t for t, w in weights.items() if w < 1 and len(t) > 2}
    return {
        "question": question, "sources": sources, "llm_docs": [] if refuse or listing else llm_docs,
        "highlight": sorted(highlight, key=len, reverse=True)[:20], "months": months, "years": years,
        "missing": missing, "refused": refuse, "listing": listing and not refuse,
        "extractive": extractive_answer(question, sources, llm_docs, missing if refuse else [], listing),
        "ms": int((time.monotonic() - t0) * 1000),
    }


def _similar(a: Doc, b: Doc) -> bool:
    ka, kb = set(a.tf), set(b.tf)
    return len(ka & kb) / max(len(ka | kb), 1) > 0.4


def _quote_list(words: list[str]) -> str:
    q = [f"“{w}”" for w in words]
    return q[0] if len(q) == 1 else ", ".join(q[:-1]) + " or " + q[-1]


def extractive_answer(question: str, sources: list, llm_docs: list, missing: list[str], listing: bool = False) -> dict:
    if listing and sources and not missing:
        strong = [s for s in sources if s["score"] >= 0.45 * sources[0]["score"]]
        items = [f"“{s['title']}”" + (f" ({s['created']})" if s["created"] else "") + f" [{s['n']}]"
                 for s in strong]
        if len(items) == 1:
            return {"text": f"I found one matching document: {items[0]}.", "facts": []}
        return {"text": f"I found {len(items)} matching documents: " + "; ".join(items) + ".", "facts": []}
    if missing:
        text = f"I couldn't find anything mentioning {_quote_list(missing)} in your {len(LIB.docs)} documents."
        if sources:
            text += " These are the closest matches, but they probably aren't what you're looking for."
        return {"text": text, "facts": []}
    if not sources:
        return {"text": "I couldn't find anything in your documents that matches that. "
                        "Try different words, a company name, or a year.", "facts": []}
    n, _, wins = llm_docs[0] if llm_docs else (1, None, [])
    s = sources[n - 1]
    pool = " ".join(wins) if wins else s["snippet"]
    facts = []
    kinds = [(rx, kind) for rx, kind, intent in ((ID_RE, "number", NUMBER_Q), (MONEY_RE, "amount", AMOUNT_Q),
                                                 (DATE_RE, "date", DATE_Q)) if intent.search(question)]
    for rx, kind in kinds or [(MONEY_RE, "amount"), (DATE_RE, "date")]:
        for m in rx.findall(pool):
            v = " ".join(m.split())
            if v not in [f["value"] for f in facts]:
                facts.append({"kind": kind, "value": v, "n": n})
            if len([f for f in facts if f["kind"] == kind]) >= 3:
                break
    when = f" ({s['created']})" if s["created"] else ""
    return {"text": f"Best match: “{s['title']}”{when} [{n}].", "facts": facts[:5]}


# ---------------------------------------------------------------- LLM

SYSTEM_PROMPT = """You are Paperless Ask, a helpful assistant that answers questions about the user's own scanned documents (bills, receipts, insurance, tax forms).
Rules:
- Use ONLY the numbered sources provided. Never invent facts, amounts, dates or numbers.
- Answer in 1-3 short sentences. Lead with the direct answer.
- Copy amounts, dates and ID numbers exactly as written in the sources.
- Cite the source number in square brackets right after each fact, like [1] or [2].
- If the sources do not contain the answer, say plainly that you couldn't find it in the documents, then mention what the closest document is about.
- When several sources are versions of the same thing from different years (e.g. insurance cards, W-2s), use the most recent one unless the question names a specific date or year.
- The text is OCR output and may contain errors; ignore garbled fragments."""


def build_prompt(question: str, llm_docs: list, missing: list[str] = ()) -> str:
    parts = []
    for n, doc, wins in llm_docs:
        meta = [f"title: {doc.title}"]
        if doc.created:
            meta.append(f"date in Paperless: {doc.created}")
        if doc.document_type:
            meta.append(f"type: {doc.document_type}")
        if doc.correspondent:
            meta.append(f"from: {doc.correspondent}")
        flat = " ".join(doc.content.split())
        if len(flat) <= (1400 if n == llm_docs[0][0] else 500):
            body = [flat]
        else:
            body = [f"(beginning) {doc.headline}"] + [f"... {w} ..." for w in wins if w[:60] not in doc.headline]
        parts.append(f"[{n}] " + " | ".join(meta) + "\n" + "\n".join(body))
    today = date.today().strftime("%B %d, %Y")
    note = f"\nNote: no document mentions {_quote_list(list(missing))}.\n" if missing else ""
    return (f"Today is {today}.\n\nSources:\n\n" + "\n\n".join(parts) + f"\n{note}\nQuestion: {question}\n\n"
            "Reply with just the answer in 1-3 sentences, using only the sources above. "
            "End each sentence with the number of the source it came from in square brackets, e.g. [1].")


def _num_variants(s: str) -> set[str]:
    v = s.replace(",", "").rstrip(".")
    out = {v}
    if v.endswith(".00"):
        out.add(v[:-3])
    elif "." not in v:
        out.add(v + ".00")
    return out


ANSWER_DATE_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? (\d{1,2})(?:st|nd|rd|th)?,? (\d{4})\b"
    r"|\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b", re.I)


def _date_variants(m: re.Match) -> tuple[str, set[str]]:
    if m.group(1):
        mo, d, y = MONTHS[m.group(1).lower()], int(m.group(2)), int(m.group(3))
    else:
        mo, d, y = int(m.group(4)), int(m.group(5)), int(m.group(6))
        y += 2000 if y < 100 else 0
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return m.group(0), set()
    full, abbr = MONTH_NAMES[mo], MONTH_NAMES[mo][:3]
    out = {f"{full} {d} {y}", f"{full} {d:02} {y}", f"{abbr} {d} {y}", f"{abbr}. {d} {y}", f"{abbr} {d:02} {y}",
           f"{mo}/{d}/{y}", f"{mo:02}/{d:02}/{y}", f"{mo:02}/{d:02}/{y % 100:02}", f"{mo}/{d}/{y % 100:02}",
           f"{y}-{mo:02}-{d:02}", f"{mo:02}{d:02}{y}", f"{mo:02}-{d:02}-{y}"}
    return m.group(0), out


def _tidy_citations(text: str) -> str:
    """'[2] Foo bar.' -> 'Foo bar [2].'   'Foo. [1].' -> 'Foo [1].'   'Foo, [1].' -> 'Foo [1].'"""
    out = []
    for sent in re.split(r"(?<=[.!?])\s+(?=\[|\w)", text.strip()):
        m = re.match(r"^((?:\[\d+\]\s*)+)(.+)$", sent)
        if m:
            cites = "".join(re.findall(r"\[\d+\]", m.group(1)))
            body = m.group(2).rstrip()
            end = body[-1] if body[-1] in ".!?" else "."
            sent = f"{body.rstrip('.!?')} {cites}{end}"
        out.append(sent)
    text = " ".join(out)
    for _ in range(3):
        text = re.sub(r"[.,;]\s*((?:\[\d+\]\s*)+)\.?", r" \1.", text)
        text = re.sub(r"\]\s+\[", "][", text)
        text = re.sub(r"(\[\d+\])(?:\1)+", r"\1", text)
        text = re.sub(r"\s+\.", ".", text)
    return text.strip()


def _has_words(text: str) -> bool:
    return len(re.findall(r"[A-Za-z]{2,}", re.sub(r"\[[\d,\s]+\]", " ", text))) >= 3


def verify_answer(answer: str, llm_docs: list) -> dict:
    """Check every number and date in the LLM's answer against the text of the documents it was
    given. Adds a citation when the model forgot to, and reports anything it couldn't ground."""
    texts = {n: (doc.title + " " + doc.created + " " + doc.content).replace(",", "").replace("$ ", "$").lower()
             for n, doc, _ in llm_docs}
    today = date.today()
    body = re.sub(r"\[[\d,\s]+\]", " ", answer)
    unverified, cite_for = [], []

    def check(label, variants):
        hits = [n for n, t in texts.items()
                if any(re.search(rf"(?<![\d.]){re.escape(v.lower())}(?!\d)", t) for v in variants)]
        if hits:
            cite_for.append(hits[0])
        elif label not in unverified:
            unverified.append(label)

    for m in list(ANSWER_DATE_RE.finditer(body)):
        label, variants = _date_variants(m)
        if variants and today.strftime("%B %-d %Y").lower() not in {v.lower() for v in variants}:
            check(label, variants)
    body = ANSWER_DATE_RE.sub(" ", body)
    for m in NUM_RE.finditer(body):
        raw = m.group(0).rstrip(".,")
        if sum(c.isdigit() for c in raw) >= 3:
            check(raw, _num_variants(raw))

    final = _tidy_citations(answer.strip().strip('"“”').strip())
    if llm_docs and not re.search(r"\[\d+", final):
        cites = "".join(f"[{n}]" for n in (sorted(set(cite_for)) or [llm_docs[0][0]]))
        final = f"{final[:-1]} {cites}{final[-1]}" if final[-1:] in (".", "!", "?") else f"{final} {cites}"
    return {"answer": final, "unverified": unverified}


LLM_LOCK = asyncio.Lock()


def ollama_options(**opts) -> dict:
    if OLLAMA_THREADS:
        opts["num_thread"] = OLLAMA_THREADS
    return opts


async def llm_stream(client, question: str, llm_docs: list, missing: list[str] = ()):
    body = {
        "model": OLLAMA_MODEL, "stream": True, "keep_alive": OLLAMA_KEEP_ALIVE,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": build_prompt(question, llm_docs, missing)}],
        "options": ollama_options(temperature=0.1, num_ctx=4096, num_predict=220),
    }
    async with LLM_LOCK:
        async with client.stream("POST", f"{OLLAMA_URL}/api/chat", json=body,
                                 timeout=httpx.Timeout(240, connect=3)) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line:
                    continue
                msg = json.loads(line)
                if msg.get("error"):
                    raise RuntimeError(msg["error"])
                tok = msg.get("message", {}).get("content", "")
                if tok:
                    yield tok
                if msg.get("done"):
                    log.info("llm: prompt %s tok in %.1fs, answer %s tok in %.1fs",
                             msg.get("prompt_eval_count"), (msg.get("prompt_eval_duration") or 0) / 1e9,
                             msg.get("eval_count"), (msg.get("eval_duration") or 0) / 1e9)
                    return


async def ollama_status(client) -> dict:
    try:
        r = await client.get(f"{OLLAMA_URL}/api/ps", timeout=2)
        r.raise_for_status()
        loaded = [m.get("name") for m in r.json().get("models", [])]
        return {"ok": True, "model": OLLAMA_MODEL, "loaded": OLLAMA_MODEL in loaded}
    except Exception as e:
        return {"ok": False, "model": OLLAMA_MODEL, "error": type(e).__name__}


async def warm_model(client):
    try:
        r = await client.post(f"{OLLAMA_URL}/api/generate", timeout=120, json={
            "model": OLLAMA_MODEL, "prompt": "ok", "keep_alive": OLLAMA_KEEP_ALIVE,
            "options": ollama_options(num_predict=1)})
        r.raise_for_status()
        log.info("ollama model %s loaded", OLLAMA_MODEL)
    except Exception as e:
        log.warning("ollama warm-up failed: %s", e)


# ---------------------------------------------------------------- app

STATE = {}
HTTP_TRANSPORT = None  # tests swap in an httpx.MockTransport


def read_token() -> str:
    """Paperless API token from PAPERLESS_TOKEN, or from the file named by PAPERLESS_TOKEN_FILE
    (e.g. a Docker secret). Use a token for a read-only Paperless user; see the README."""
    if PAPERLESS_TOKEN:
        tok, where = PAPERLESS_TOKEN, "PAPERLESS_TOKEN"
    elif TOKEN_FILE:
        tok, where = Path(TOKEN_FILE).read_text().strip(), f"token file {TOKEN_FILE}"
    else:
        raise RuntimeError("set PAPERLESS_TOKEN (or PAPERLESS_TOKEN_FILE) to a Paperless API token")
    if not re.fullmatch(r"[0-9A-Za-z]{20,128}", tok):
        raise RuntimeError(f"{where} doesn't look like a Paperless API token")
    return tok


async def background():
    """Keep the document cache fresh and the model loaded, so the first question is fast."""
    while True:
        try:
            await LIB.refresh(STATE["pl"], force=time.time() - LIB.refreshed_at > REFRESH_SECONDS)
        except Exception as e:
            LIB.error = f"{type(e).__name__}: {e}"
            log.warning("background refresh failed: %s", LIB.error)
        st = await ollama_status(STATE["ol"])
        if OLLAMA_WARM and st["ok"] and not st["loaded"] and not LLM_LOCK.locked():
            await warm_model(STATE["ol"])
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_app):
    STATE["pl"] = httpx.AsyncClient(headers={"Authorization": f"Token {read_token()}",
                                             "Accept": "application/json"}, transport=HTTP_TRANSPORT)
    STATE["ol"] = httpx.AsyncClient(transport=HTTP_TRANSPORT)
    STATE["page"] = render_page()
    STATE["thumbs"] = OrderedDict()
    STATE["started"] = time.time()
    log.info("paperless-ask %s starting: paperless=%s ollama=%s model=%s login=%s", __version__,
             PAPERLESS_URL, OLLAMA_URL, OLLAMA_MODEL, "on" if ASK_PASSWORD else "off")
    bg = asyncio.create_task(background())
    yield
    bg.cancel()
    await STATE["pl"].aclose()
    await STATE["ol"].aclose()


def render_page() -> str:
    """index.html with the per-install settings the browser needs."""
    page = (STATIC_DIR / "index.html").read_text()
    config = {"suggestions": SUGGESTIONS}
    tag = "<script>window.PAPERLESS_ASK = " + json.dumps(config).replace("</", "<\\/") + ";</script>"
    return page.replace("<!--CONFIG-->", tag, 1)


app = FastAPI(title="Paperless Ask", version=__version__, docs_url=None, redoc_url=None,
              openapi_url=None, lifespan=lifespan)

OPEN_PATHS = {"/healthz", "/manifest.webmanifest", "/icon.png", "/apple-touch-icon.png"}


def _authorized(header: str) -> bool:
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        user, _, password = base64.b64decode(value).decode().partition(":")
    except Exception:
        return False
    return secrets.compare_digest(user.encode(), ASK_USERNAME.encode()) & \
        secrets.compare_digest(password.encode(), ASK_PASSWORD.encode())


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if ASK_PASSWORD and request.url.path not in OPEN_PATHS \
            and not _authorized(request.headers.get("authorization", "")):
        return Response("Login required", status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Paperless Ask", charset="UTF-8"'})
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(STATE["page"], headers={"Cache-Control": "no-cache"})


@app.get("/healthz")
async def healthz():
    pl_ok = True
    try:
        await LIB.ensure_fresh(STATE["pl"])
    except Exception:
        pl_ok = False
    ol = await ollama_status(STATE["ol"])
    refreshed = datetime.fromtimestamp(LIB.refreshed_at).isoformat(timespec="seconds") if LIB.refreshed_at else None
    body = {
        "ok": bool(LIB.docs),
        "paperless": {"ok": pl_ok and LIB.error is None, "documents": len(LIB.docs), "refreshed": refreshed},
        "llm": ol,
        "uptime_s": int(time.time() - STATE["started"]),
        "version": __version__,
    }
    if not ASK_PASSWORD:
        body["paperless"].update(url=PAPERLESS_URL, error=LIB.error)
    return JSONResponse(body, status_code=200 if body["ok"] else 503)


@app.get("/api/stats")
async def stats():
    try:
        await LIB.ensure_fresh(STATE["pl"])
    except Exception:
        pass
    ol = await ollama_status(STATE["ol"])
    return {"documents": len(LIB.docs), "refreshed": int(LIB.refreshed_at), "llm": ol["ok"],
            "model": OLLAMA_MODEL, "paperless": PAPERLESS_LINK_URL}


def sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def public(r: dict) -> dict:
    return {k: v for k, v in r.items() if k != "llm_docs"}


@app.get("/api/ask/stream")
async def ask_stream(q: str = Query(..., min_length=1, max_length=500)):
    q = " ".join(q.split())

    async def events():
        t0 = time.monotonic()
        try:
            await LIB.ensure_fresh(STATE["pl"])
            r = await retrieve(STATE["pl"], q)
        except Exception:
            log.exception("retrieval failed")
            yield sse("fatal", {"message": "Couldn't reach Paperless right now. Is it running?"})
            return
        log.info("ask %r -> %d sources (%d ms)", q[:120], len(r["sources"]), r["ms"])
        yield sse("sources", {**public(r), "library": len(LIB.docs)})
        if not r["llm_docs"]:
            yield sse("done", {"mode": "list" if r["listing"] else "none", "ms": int((time.monotonic() - t0) * 1000)})
            return
        yield sse("stage", {"stage": "writing", "busy": LLM_LOCK.locked()})
        got, text = False, ""
        try:
            async for tok in llm_stream(STATE["ol"], q, r["llm_docs"], r["missing"]):
                got = True
                text += tok
                yield sse("token", {"t": tok})
            if not _has_words(text):
                raise RuntimeError(f"no usable answer: {text!r}")
            v = verify_answer(text, r["llm_docs"])
            if v["unverified"]:
                log.info("unverified numbers in answer: %s", v["unverified"])
            yield sse("done", {"mode": "llm", "model": OLLAMA_MODEL, **v,
                               "ms": int((time.monotonic() - t0) * 1000)})
        except Exception as e:
            log.warning("llm failed (%s): %s", type(e).__name__, e)
            yield sse("llm_error", {"partial": got,
                                    "message": "The local AI didn't answer, so here are the best matching passages."})
            yield sse("done", {"mode": "extractive", "ms": int((time.monotonic() - t0) * 1000)})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/ask")
async def ask_json(payload: dict):
    """Non-streaming variant for curl / scripts: {"q": "...", "no_llm": false}."""
    q = " ".join(str(payload.get("q", "")).split())[:500]
    if not q:
        return JSONResponse({"error": "missing q"}, status_code=400)
    t0 = time.monotonic()
    await LIB.ensure_fresh(STATE["pl"])
    r = await retrieve(STATE["pl"], q)
    out = public(r)
    out["mode"], out["answer"], out["unverified"] = "list" if r["listing"] else "none", r["extractive"]["text"], []
    if payload.get("debug") and r["llm_docs"]:
        out["prompt"] = build_prompt(q, r["llm_docs"], r["missing"])
    if r["llm_docs"]:
        out["mode"] = "extractive"
        if not payload.get("no_llm"):
            try:
                text = "".join([t async for t in llm_stream(STATE["ol"], q, r["llm_docs"], r["missing"])])
                if not _has_words(text):
                    raise RuntimeError(f"no usable answer: {text!r}")
                out.update(verify_answer(text, r["llm_docs"]))
                out["raw_answer"] = text
                out["mode"] = "llm"
            except Exception as e:
                log.warning("llm failed (%s): %s", type(e).__name__, e)
    out["total_ms"] = int((time.monotonic() - t0) * 1000)
    log.info("ask(json) %r -> %s, %d sources, %d ms", q[:120], out["mode"], len(r["sources"]), out["total_ms"])
    return out


@app.get("/thumb/{doc_id}")
async def thumb(doc_id: int):
    if doc_id not in LIB.docs:
        return Response(status_code=404)
    cache = STATE["thumbs"]
    if doc_id not in cache:
        try:
            r = await STATE["pl"].get(f"{PAPERLESS_URL}/api/documents/{doc_id}/thumb/", timeout=15)
            r.raise_for_status()
        except Exception:
            return Response(status_code=502)
        cache[doc_id] = (r.content, r.headers.get("content-type", "image/webp"))
        while len(cache) > 300:
            cache.popitem(last=False)
    body, ctype = cache[doc_id]
    return Response(body, media_type=ctype, headers={"Cache-Control": "private, max-age=3600"})


def _png(size: int) -> bytes:
    """App icon: violet->cyan gradient with a white document glyph (no image libraries needed)."""
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            t = (x + y) / (2 * size)
            px = (int(124 - 90 * t), int(58 + 153 * t), int(237 + t))
            u, v = x / size, y / size
            in_doc = 0.28 < u < 0.72 and 0.2 < v < 0.8 and not (u > 0.6 and v < 0.2 + (u - 0.6))
            line = in_doc and 0.36 < u < 0.64 and any(abs(v - c) < 0.022 for c in (0.42, 0.52, 0.62)) \
                and not (v > 0.6 and u > 0.54)
            if line:
                px = (124, 92, 240)
            elif in_doc:
                px = (255, 255, 255)
            row += bytes(px)
        rows.append(bytes(row))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


ICON = _png(180)


@app.get("/apple-touch-icon.png")
@app.get("/icon.png")
async def icon():
    return Response(ICON, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/manifest.webmanifest")
async def manifest():
    return JSONResponse({"name": "Paperless Ask", "short_name": "Ask", "start_url": "/", "display": "standalone",
                         "background_color": "#060814", "theme_color": "#060814",
                         "icons": [{"src": "/icon.png", "sizes": "180x180", "type": "image/png"}]})
