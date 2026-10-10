"""'Coming up': expiry and due dates found in documents, for the start page, /api/upcoming and an
iCal feed.

Dates come from two places: labelled dates found by pattern ("Expiration Date: 10/22/2026",
"Payment due by Oct 5, 2025"), which needs no model, and the `due`/`expires` dates on fact cards.
"""
import re
from datetime import date, datetime, timedelta, timezone

from .textutil import ANSWER_DATE_RE, match_to_date, money

_LABELS = {
    "expires": r"expiration date|expiry date|exp\.? date|expires(?: on)?|expiring(?: on)?|valid (?:until|thru|through)"
               r"|coverage ends|renewal date|renews(?: on)?",
    "due": r"payment due date|payment due(?: by| on)?|due date|due by|due on|pay(?:ment)? by|please pay by"
           r"|amount due by|balance due by",
}
_DATE = ANSWER_DATE_RE.pattern
PATTERNS = [(label, re.compile(rf"\b(?:{words})\b[^\d\n]{{0,20}}?(?:{_DATE})", re.I)) for label, words in _LABELS.items()]


def pattern_dates(text: str) -> set[tuple[str, date]]:
    """Labelled dates written in the document itself: {('expires', date(2026, 10, 22)), ...}."""
    flat = " ".join(text.split())
    out = set()
    for label, rx in PATTERNS:
        for m in rx.finditer(flat):
            d = ANSWER_DATE_RE.search(m.group(0))
            when = match_to_date(d) if d else None
            if when:
                out.add((label, when))
    return out


def doc_dates(doc, card: dict | None) -> set[tuple[str, date]]:
    out = pattern_dates(doc.content)
    for d in (card or {}).get("dates", []):
        if d["label"] in ("due", "expires"):
            out.add((d["label"], date.fromisoformat(d["date"])))
    return out


def _series(doc, card) -> str:
    """Documents in the same series (this year's and last year's insurance card) share this key."""
    who = (card or {}).get("vendor") or doc.correspondent or re.sub(r"[\d_\-\s]+", " ", doc.title).strip()
    return f"{(card or {}).get('kind', '')}|{who.lower()}"


def _when(days: int, d: date) -> str:
    day = d.strftime("%a %b %-d")
    if days == 0:
        return f"today ({day})"
    if days == 1:
        return f"tomorrow ({day})"
    return f"in {days} days ({day})"


def upcoming(docs, cards, today: date, days: int, done_tags=("paid",), link=lambda doc_id: "",
             alert_days=(14, 3, 1, 0)) -> dict:
    """Due and expiry dates from today to today+days, soonest first, plus the ones to alert about today."""
    done = {t.lower() for t in done_tags}
    dated = [(doc, cards.get(doc.id), doc_dates(doc, cards.get(doc.id))) for doc in docs]
    latest_expiry: dict[str, date] = {}
    for doc, card, dates in dated:
        for label, when in dates:
            if label == "expires":
                key = _series(doc, card)
                latest_expiry[key] = max(when, latest_expiry.get(key, when))
    items = []
    for doc, card, dates in dated:
        for label, when in sorted(dates, key=lambda x: x[1]):
            left = (when - today).days
            if not 0 <= left <= days:
                continue
            if label == "due" and done & {t.lower() for t in doc.tags}:
                continue
            if label == "expires" and latest_expiry[_series(doc, card)] > when:
                continue  # a newer document (the renewal) runs longer
            name = (card or {}).get("vendor") or doc.correspondent or doc.title
            amount = (card or {}).get("total") if label == "due" else None
            if label == "due":
                text = f"{name}: {money(amount) + ' ' if amount else ''}due {_when(left, when)}"
            else:
                what = {"insurance": "insurance", "id": "ID"}.get((card or {}).get("kind"), "document")
                text = f"{name} {what} expires {_when(left, when)}"
            items.append({"id": doc.id, "title": doc.title, "label": label, "date": when.isoformat(),
                          "days_left": left, "vendor": name, "amount": amount, "url": link(doc.id), "text": text})
    items.sort(key=lambda i: (i["date"], i["label"], i["id"]))
    # one entry per document and label (OCR sometimes repeats the same notice with two dates)
    seen, unique = set(), []
    for i in items:
        if (i["id"], i["label"]) not in seen:
            seen.add((i["id"], i["label"]))
            unique.append(i)
    alerts = [i for i in unique if i["days_left"] in set(alert_days)]
    return {"today": today.isoformat(), "days": days, "items": unique, "alerts": alerts}


def _ics_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode()) > 74:
            out.append(cur)
            cur = " "
        cur += ch
    out.append(cur)
    return "\r\n".join(out)


def ics(items: list[dict], alert_days=(14, 3, 1, 0), host: str = "paperless-ask", now: datetime | None = None) -> str:
    """An iCalendar feed: one all-day event per item, with a 09:00 alarm on each alert day."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Paperless Ask//Coming up//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:Paperless: coming up", "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
             "X-PUBLISHED-TTL:PT6H"]
    for i in items:
        d = date.fromisoformat(i["date"])
        summary = (f"Due: {i['vendor']}" + (f" {money(i['amount'])}" if i.get("amount") else "")
                   if i["label"] == "due" else f"Expires: {i['vendor']}")
        lines += ["BEGIN:VEVENT", f"UID:{i['id']}-{i['label']}-{d:%Y%m%d}@{host}", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}",
                  f"SUMMARY:{_ics_text(summary)}", f"DESCRIPTION:{_ics_text(i['title'] + (chr(10) + i['url'] if i['url'] else ''))}",
                  "TRANSP:TRANSPARENT"]
        if i.get("url"):
            lines.append(f"URL:{i['url']}")
        for n in sorted(set(alert_days), reverse=True):
            minutes = n * 24 * 60 - 9 * 60  # 09:00 local time, n days before
            trigger = f"-PT{minutes}M" if minutes > 0 else f"PT{-minutes}M"
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_ics_text(summary)}",
                      f"TRIGGER:{trigger}", "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
