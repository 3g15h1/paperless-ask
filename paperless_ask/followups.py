"""Follow-up questions without server-side sessions: the browser sends the previous question back,
and this decides whether the new one continues it and what to search for."""
import re

from .textutil import MONTH_NAMES

CONTINUES = re.compile(r"^\s*(?:ok(?:ay)?,?\s+)?(and|also|plus|what about|how about|what of|same for|same with|then)\b", re.I)
REFERS_BACK = re.compile(r"\b(it|its|it's|that|that one|those|them|they|this one|the other|another|other one|"
                         r"last year'?s|previous|same)\b", re.I)
OLDER = re.compile(r"\b(last year'?s?|previous|prior|older|old|before|earlier|the year before)\b", re.I)
_MONTH_WORDS = "|".join(sorted({m for m in MONTH_NAMES[1:]} | {m[:3] for m in MONTH_NAMES[1:]} | {"sept"},
                               key=len, reverse=True))
DATE_WORDS = re.compile(rf"\b(?:(?:19|20)\d\d|(?:{_MONTH_WORDS})\.?|this year|last year'?s?|next year)\b", re.I)


def plan(question: str, prev_q: str | None, content_words: int) -> dict:
    """{'search': text to search for, 'followup': bool, 'older': bool}.

    content_words: how many content words the new question has (after stop words). A question with
    at most one ("and the other car?") leans on the previous one."""
    older = bool(OLDER.search(question))
    prev_q = " ".join((prev_q or "").split())[:500]
    if not prev_q or not (CONTINUES.search(question) or REFERS_BACK.search(question) or content_words <= 1):
        return {"search": question, "followup": False, "older": older}
    if DATE_WORDS.search(question):
        prev_q = " ".join(DATE_WORDS.sub(" ", prev_q).split())  # "and in 2024?" replaces the old period
    return {"search": f"{prev_q} {question}", "followup": True, "older": older}
