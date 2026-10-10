"""Fact cards: each document's key facts, extracted once by the local model and kept on disk.

The model's output is never trusted as is. Every amount, date and ID number must appear in the
document's own text, or it is dropped (see ground()). Cards are redone when the document changes
in Paperless or when EXTRACTOR_VERSION changes.
"""
import json
import logging
import os
import re
import tempfile
import time
from datetime import date
from pathlib import Path

from .textutil import date_variants, grounded, normalize, num_variants, parse_amount

log = logging.getLogger("paperless-ask")

EXTRACTOR_VERSION = 1  # bump when prompts or grounding change, so existing cards are redone
KINDS = ["receipt", "invoice", "bill", "statement", "insurance", "tax", "medical", "pay", "contract", "id",
         "letter", "other"]
CATEGORIES = ["medical", "insurance", "utilities", "shopping", "home", "vehicle", "tax", "income", "travel",
              "food", "finance", "other"]
DATE_LABELS = ["due", "expires", "issued", "paid", "service", "effective", "other"]

SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": KINDS},
        "category": {"type": "string", "enum": CATEGORIES},
        "vendor": {"type": "string"},
        "summary": {"type": "string"},
        "total": {"type": ["string", "null"]},
        "dates": {"type": "array", "items": {
            "type": "object", "required": ["label", "date"],
            "properties": {"label": {"type": "string", "enum": DATE_LABELS}, "date": {"type": "string"}}}},
        "ids": {"type": "array", "items": {
            "type": "object", "required": ["label", "value"],
            "properties": {"label": {"type": "string"}, "value": {"type": "string"}}}},
    },
    "required": ["kind", "category", "vendor", "summary", "total", "dates", "ids"],
}

SYSTEM_PROMPT = """Extract the key facts from one scanned document (OCR text, may contain errors). Reply with JSON only.
- kind: what the document is. category: what area of life it belongs to.
- vendor: the company, shop, insurer, clinic or agency that issued it.
- summary: one short sentence saying what the document is.
- total: the final amount paid or due, copied exactly as written, or null if there is none.
- dates: the important dates and what they mean (due = payment due, expires = coverage/validity ends), written as YYYY-MM-DD.
- ids: policy, account, invoice, order, member or reference numbers, copied exactly. Never put names here.
Leave out anything that is not in the text."""


def messages(title: str, content: str, max_chars: int) -> list[dict]:
    text = " ".join(content.split())[:max_chars]
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Title: {title}\n\n{text}"}]


# Words that say what kind of business it is, not which one ("Acme Insurance Group" must match "acme")
_GENERIC_VENDOR_WORDS = set("""the and inc llc ltd corp corporation company group services service insurance bank
card cards financial medical center centre clinic store stores shop family health care department dept""".split())


def _squash(s: str) -> str:
    return re.sub(r"[\s\-./]", "", s).lower()


def ground(raw: dict, text: str, correspondent: str | None = None, title: str = "") -> dict:
    """Keep only what the document's text supports. Returns the card fields plus `dropped`, a list of
    human-readable notes about what was thrown away (handy when checking the extractor).
    ID numbers must be in the text itself; the title (often a scanner file name) only counts for the
    vendor, amounts and dates."""
    squashed = _squash(text)
    norm = normalize(f"{title}\n{text}")
    dropped = []

    kind = raw.get("kind") if raw.get("kind") in KINDS else "other"
    category = raw.get("category") if raw.get("category") in CATEGORIES else "other"

    vendor = " ".join(str(raw.get("vendor") or "").split())[:80]
    words = [w.lower() for w in re.findall(r"[A-Za-z]{3,}", vendor)]
    words = [w for w in words if w not in _GENERIC_VENDOR_WORDS] or words
    if vendor and not all(re.search(rf"\b{re.escape(w)}", norm) for w in words):
        dropped.append(f"vendor {vendor!r}")
        vendor = ""
    vendor = vendor or (correspondent or "")

    total_text = raw.get("total")
    total = parse_amount(total_text) if total_text not in (None, "", "null", "None") else None
    if total_text not in (None, "", "null", "None"):
        if total is None or total <= 0 or not grounded(norm, num_variants(f"{total:.2f}")):
            dropped.append(f"total {total_text!r}")
            total = None

    dates, seen = [], set()
    for d in raw.get("dates") or []:
        label = d.get("label") if d.get("label") in DATE_LABELS else "other"
        try:
            when = date.fromisoformat(str(d.get("date", "")).strip()[:10])
        except ValueError:
            dropped.append(f"date {d.get('date')!r}")
            continue
        if not grounded(norm, date_variants(when.year, when.month, when.day)):
            dropped.append(f"{label} date {when.isoformat()}")
            continue
        if (label, when) not in seen:
            seen.add((label, when))
            dates.append({"label": label, "date": when.isoformat()})

    ids, seen_ids = [], set()
    for i in raw.get("ids") or []:
        value = " ".join(str(i.get("value") or "").split()).strip("()[]{}:;,.#")
        label = " ".join(str(i.get("label") or "").split())[:40] or "number"
        if len(value) < 4 or not re.search(r"\d", value) or _squash(value) not in squashed:
            dropped.append(f"id {label}={value!r}")
            continue
        if _squash(value) not in seen_ids:
            seen_ids.add(_squash(value))
            ids.append({"label": label, "value": value})

    summary = " ".join(str(raw.get("summary") or "").split())[:200]
    return {"kind": kind, "category": category, "vendor": vendor, "summary": summary, "total": total,
            "dates": dates, "ids": ids, "dropped": dropped}


async def extract(client, ollama_url: str, model: str, options: dict, keep_alive: str, title: str, content: str,
                  correspondent: str | None, max_chars: int) -> dict:
    """Ask the model for a card (structured output) and ground it against the text."""
    body = {"model": model, "stream": False, "format": SCHEMA, "keep_alive": keep_alive,
            "messages": messages(title, content, max_chars), "options": options}
    r = await client.post(f"{ollama_url}/api/chat", json=body, timeout=600)
    r.raise_for_status()
    msg = r.json()
    if msg.get("error"):
        raise RuntimeError(msg["error"])
    raw = json.loads(msg["message"]["content"])
    return ground(raw, content, correspondent, title)


class FactStore:
    """Cards keyed by document id, saved as JSON (atomically) in DATA_DIR."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.cards: dict[int, dict] = {}
        try:
            data = json.loads(self.path.read_text())
            self.cards = {int(k): v for k, v in data.get("cards", {}).items()}
        except FileNotFoundError:
            pass
        except Exception as e:  # a damaged file shouldn't stop the app; it is rebuilt over time
            log.warning("fact store %s unreadable (%s); starting empty", self.path, e)

    def get(self, doc_id: int) -> dict | None:
        return self.cards.get(doc_id)

    def stale(self, doc_id: int, modified: str, model: str) -> bool:
        c = self.cards.get(doc_id)
        return not c or c.get("modified") != modified or c.get("version") != EXTRACTOR_VERSION \
            or c.get("model") != model

    def put(self, doc_id: int, card: dict, modified: str, model: str) -> None:
        self.cards[doc_id] = {**card, "modified": modified, "model": model, "version": EXTRACTOR_VERSION,
                              "extracted_at": int(time.time())}
        self.save()

    def prune(self, keep_ids) -> None:
        gone = set(self.cards) - set(keep_ids)
        if gone:
            for i in gone:
                del self.cards[i]
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".facts-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"version": EXTRACTOR_VERSION, "cards": {str(k): v for k, v in sorted(self.cards.items())}},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)
