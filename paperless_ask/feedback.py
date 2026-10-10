"""Thumbs up/down on answers: an append-only JSON-lines log in DATA_DIR and a review page."""
import html
import json
import time
from pathlib import Path

LIMITS = {"id": 64, "q": 500, "context_q": 1000, "comment": 1000, "answer": 2000, "mode": 20}


def clean(payload: dict) -> dict:
    """Keep known fields, capped in size; raises ValueError for a bad vote."""
    vote = payload.get("vote")
    if vote not in ("up", "down"):
        raise ValueError("vote must be 'up' or 'down'")
    out = {"vote": vote, "ts": int(time.time())}
    for k, n in LIMITS.items():
        v = payload.get(k)
        if v not in (None, ""):
            out[k] = str(v)[:n]
    if not out.get("id") or not out.get("q"):
        raise ValueError("id and q are required")
    doc = payload.get("doc")
    if isinstance(doc, int):
        out["doc"] = doc
    out["sources"] = [{"id": int(s.get("id")), "title": str(s.get("title", ""))[:200]}
                      for s in (payload.get("sources") or [])[:10] if isinstance(s, dict) and str(s.get("id", "")).isdigit()]
    out["missing"] = [str(w)[:40] for w in (payload.get("missing") or [])[:10]]
    return out


class FeedbackLog:
    def __init__(self, path: Path):
        self.path = Path(path)

    def add(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def entries(self) -> list[dict]:
        """Newest first; a later line with the same id (e.g. a comment added) replaces the earlier one."""
        by_id = {}
        try:
            with self.path.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    update = {k: v for k, v in e.items() if v not in ([], "", None)}
                    by_id[e.get("id")] = {**by_id.get(e.get("id"), {}), **update}
        except FileNotFoundError:
            pass
        return sorted(by_id.values(), key=lambda e: -e.get("ts", 0))

    def summary(self) -> dict:
        es = self.entries()
        return {"up": sum(e["vote"] == "up" for e in es), "down": sum(e["vote"] == "down" for e in es), "total": len(es)}


def render(entries: list[dict], link) -> str:
    """The review page: answers marked wrong first, newest first."""
    esc = html.escape
    rows = []
    for e in sorted(entries, key=lambda e: (e["vote"] != "down", -e.get("ts", 0))):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.get("ts", 0)))
        srcs = ", ".join(f'<a href="{esc(link(s["id"]))}">{esc(s["title"] or "#" + str(s["id"]))}</a>'
                         for s in e.get("sources", [])) or "none"
        hint = ""
        if e.get("missing"):
            words = ", ".join(esc(w) for w in e["missing"])
            example = esc(e["missing"][0].lower())
            hint = (f'<div class="hint">Words found in no document: <b>{words}</b>. If a document uses another word for it, '
                    f'add a synonym, e.g. <code>ASK_SYNONYMS={example}=otherword</code></div>')
        extra = []
        if e.get("context_q") and e["context_q"] != e.get("q"):
            extra.append(f'searched as “{esc(e["context_q"])}”')
        if e.get("doc"):
            extra.append(f'asked about <a href="{esc(link(e["doc"]))}">document #{e["doc"]}</a>')
        if e.get("mode"):
            extra.append(f'answer type: {esc(e["mode"])}')
        rows.append(f"""<article class="{e['vote']}">
  <div class="top"><span class="vote">{'👎' if e['vote'] == 'down' else '👍'}</span><span class="q">{esc(e['q'])}</span><span class="when">{when}</span></div>
  {f'<div class="meta">{" · ".join(extra)}</div>' if extra else ''}
  <div class="a">{esc(e.get('answer', ''))}</div>
  <div class="meta">Sources: {srcs}</div>
  {f'<div class="comment">“{esc(e["comment"])}”</div>' if e.get('comment') else ''}
  {hint}
</article>""")
    up, down = sum(e["vote"] == "up" for e in entries), sum(e["vote"] == "down" for e in entries)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><link rel="icon" href="/icon.png"><title>Paperless Ask · feedback</title>
<style>
body {{ margin: 0; background: #060814; color: #eef0fb; font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
main {{ max-width: 860px; margin: 0 auto; padding: 28px 18px 60px; }}
h1 {{ font-size: 22px; margin: 0 0 4px; }} .sum {{ color: #98a1c6; margin-bottom: 22px; }} a {{ color: #a5b4fc; }}
article {{ border: 1px solid rgba(255,255,255,.1); border-radius: 16px; padding: 14px 16px; margin-bottom: 12px; background: rgba(255,255,255,.04); }}
article.down {{ border-color: rgba(250,204,21,.35); }}
.top {{ display: flex; gap: 10px; align-items: baseline; }} .q {{ font-weight: 600; flex: 1; }} .when, .meta {{ color: #5f6890; font-size: 12.5px; }}
.a {{ margin: 8px 0; color: #c7cdee; }} .comment {{ margin-top: 8px; color: #fde68a; }}
.hint {{ margin-top: 8px; font-size: 13px; color: #98a1c6; }} code {{ background: rgba(255,255,255,.08); padding: 1px 5px; border-radius: 6px; }}
</style></head><body><main>
<h1>Answer feedback</h1><div class="sum">{down} marked wrong · {up} marked right · <a href="/">back to Paperless Ask</a></div>
{''.join(rows) or '<p class="sum">No feedback yet. Use 👍/👎 under an answer.</p>'}
</main></body></html>"""
