"""Text helpers shared by the app, fact cards, reminders and totals: dates, amounts, and checking
that a value really appears in a document's text."""
import re
from datetime import date

MONTH_NAMES = ["", "january", "february", "march", "april", "may", "june", "july", "august",
               "september", "october", "november", "december"]
MONTHS = {}
for _i, _name in enumerate(MONTH_NAMES[1:], 1):
    MONTHS[_name] = _i
    MONTHS[_name[:3]] = _i
MONTHS["sept"] = 9

MONEY_RE = re.compile(r"\$\s?\(?\d[\d,]*(?:\.\d{2})?\)?|(?<![\d.,/-])\d{1,3}(?:,?\d{3})*\.\d{2}(?![\d.])")
DATE_RE = re.compile(
    r"\b(?:\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? \d{1,2},? \d{4})\b", re.I)
# Dates as written in prose or forms, with the parts captured (used to normalise them)
ANSWER_DATE_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? (\d{1,2})(?:st|nd|rd|th)?,? (\d{4})\b"
    r"|\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b"
    r"|\b(\d{4})-(\d{2})-(\d{2})\b", re.I)


def num_variants(s: str) -> set[str]:
    """Ways an amount may be written: '1,099.00' -> {'1099.00', '1099'} (commas are normalised away)."""
    v = s.replace(",", "").replace("$", "").strip().rstrip(".")
    out = {v}
    if v.endswith(".00"):
        out.add(v[:-3])
    elif "." not in v:
        out.add(v + ".00")
    return out


def date_variants(y: int, mo: int, d: int) -> set[str]:
    """Ways a date may be written in a document (lower case, commas removed)."""
    full, abbr = MONTH_NAMES[mo], MONTH_NAMES[mo][:3]
    return {f"{full} {d} {y}", f"{full} {d:02} {y}", f"{abbr} {d} {y}", f"{abbr}. {d} {y}", f"{abbr} {d:02} {y}",
            f"{mo}/{d}/{y}", f"{mo:02}/{d:02}/{y}", f"{mo:02}/{d:02}/{y % 100:02}", f"{mo}/{d}/{y % 100:02}",
            f"{y}-{mo:02}-{d:02}", f"{mo:02}{d:02}{y}", f"{mo:02}-{d:02}-{y}"}


def match_to_date(m: re.Match) -> date | None:
    """A date from an ANSWER_DATE_RE match, or None if it isn't a real date."""
    try:
        if m.group(1):
            mo, d, y = MONTHS[m.group(1).lower()], int(m.group(2)), int(m.group(3))
        elif m.group(4):
            mo, d, y = int(m.group(4)), int(m.group(5)), int(m.group(6))
            y += 2000 if y < 100 else 0
        else:
            y, mo, d = int(m.group(7)), int(m.group(8)), int(m.group(9))
        return date(y, mo, d)
    except (ValueError, KeyError):
        return None


def normalize(text: str) -> str:
    """Lower case, commas removed, '$ 5' -> '$5': the form grounded() searches."""
    return text.replace(",", "").replace("$ ", "$").lower()


def grounded(norm_text: str, variants) -> bool:
    """True if any variant appears in normalize()d text as a whole number/date (not inside a longer one)."""
    return any(re.search(rf"(?<![\d.]){re.escape(v.lower())}(?!\d)", norm_text) for v in variants)


def parse_amount(s) -> float | None:
    """'$1,099.00' -> 1099.0; None for anything that isn't a plain amount."""
    if s is None:
        return None
    m = re.fullmatch(r"\s*\(?\$?\s*(\d[\d,]*(?:\.\d{1,2})?)\)?\s*(?:usd)?\s*", str(s), re.I)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def money(v: float) -> str:
    return f"${v:,.2f}"
