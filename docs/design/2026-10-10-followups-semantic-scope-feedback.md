# Design: follow-up questions, search by meaning, "ask about this document", and answer feedback

Status: accepted, 2026-10-10. Target release: 1.2.0. Builds on
[fact cards, reminders and totals](2026-10-10-facts-reminders-totals.md).

## Goals

1. **Follow-ups.** "and last year's?" or "what about the other car?" are understood in the context
   of the previous question.
2. **Search by meaning.** A small embedding model finds documents that use different words from
   the question, like "grass cutting" for a lawn service invoice or "smartphone financing" for an
   iPhone payment notice.
3. **Ask about this document.** A button on a source card scopes the following questions to that
   document only.
4. **Feedback.** Thumbs up/down on answers, an optional comment, and a review page listing the
   answers marked wrong. The page shows what was asked, what was cited, and which words matched
   nothing, which suggests synonyms to add.

Principles carried over: everything is optional and degrades cleanly; the server stays stateless
for conversations; nothing leaves the user's network; the model never decides on its own what
counts as found.

## 1. Follow-ups (stateless)

The browser keeps the last question and answer and sends them along: `prev_q`/`prev_a` on
`/api/ask/stream`, or `"prev": {"q", "a"}` in `POST /api/ask`. The UI shows a removable
"Follow-up to …" chip above the ask box. Asking a suggestion or opening a saved answer starts a
new topic.

The server treats a question as a follow-up only when `prev_q` is given and the question either
starts with a continuation ("and …", "what about …", "how about …", "same for …") or refers back to
it:
- pronouns like it, that, those, them;
- "the other", "another";
- "last year's", "previous", "same";
- or the question has at most one content word.

Anything else is a new question even with a chip showing, which makes a stale chip harmless.

For a follow-up:
- **Retrieval** searches `prev_q + " " + q`. If the follow-up names a date or period (a year, a
  month, "last year"), the previous question's date words are dropped first, so "and in 2024?"
  replaces 2025 instead of adding to it.
- **Recency.** "Latest/current" handling is turned off when the follow-up asks for something older
  ("last year's", "previous", "old", "before"), so last year's card isn't filtered out as an older
  near-copy.
- **The model's prompt** gets the earlier question and answer (answer capped at 300 characters),
  so it can resolve "it" and "the other one".
- **Instant answers** take their intent (expiry, due date, number, amount) from the follow-up's own
  words first, falling back to the combined question.
- The response includes `context_q`, the combined question. The browser sends that as `prev_q`
  next time, so follow-ups chain.

"last year" / "this year" become calendar years everywhere, not just in totals.

## 2. Search by meaning

- **Model:** `EMBED_MODEL` in the configured Ollama, `/api/embed`. The default is
  `nomic-embed-text` (274 MB; about 380 MB in memory). `all-minilm` (46 MB) is lighter but ranked
  worse (see "Calibration"). `EMBED_MODEL=off` disables it.
  If the model isn't installed, the feature turns itself off, logs `ollama pull <model>`, and
  checks again every 30 minutes.
- **Index:** each document's text is cut into passages of about 600 characters (at most
  `EMBED_MAX_CHUNKS`, default 40), embedded once in the background, and saved to
  `DATA_DIR/embeddings.npz` with each document's `modified` time and the model. Changed
  documents are redone and deleted ones dropped. It uses the same idle gating as fact cards, but
  doesn't wait for quiet hours: embedding is cheap.
- **At question time:** the question is embedded (3 s timeout; on failure the app carries on
  without semantic search). A document's semantic score is its best passage's cosine similarity.
- **Ranking:** the best semantic match, and any of the top 3 within `SEMANTIC_BAND` (0.06) of it,
  gets up to +0.5 on top of the word-based score, where the word-based top document scores 1.0.
  Only documents above a per-model floor (`SEMANTIC_MIN`) count. So meaning reorders close calls
  and adds documents that share no words with the question.
- **"Couldn't find" stays honest.** Calibration showed that unrelated questions score as high as
  real paraphrases, so semantic similarity never overrules "I couldn't find anything mentioning
  X". Instead:
  - that answer lists the closest documents in meaning, saying none of them uses the word;
  - it offers "Ask the AI" (`full=1`), which has the model read them, with the prompt still noting
    that the word appears in no document.
- Ollama keeps up to 3 models loaded by default. An install that limits Ollama to one model at a
  time would reload the chat model after every embedding, so the README says to allow two.

## 3. Ask about this document

Each source card gets an "Ask about this" button. It sets a document scope, shown as a removable
chip ("About: <title>"), and questions are sent with `doc=<id>`. With a scope:
- ranking is skipped, and the only source is that document;
- the model gets up to 3,500 characters of it (instead of 1,400) plus its best passages;
- refusals, listings and totals are off, but instant answers from its fact card still work;
- follow-ups work within the scope.

## 4. Feedback

- **Rating:** 👍/👎 buttons appear on every finished answer. 👎 opens an optional "What was wrong?"
  comment.
- **Storage:** `POST /api/feedback` appends one JSON line to `DATA_DIR/feedback.jsonl`. A line
  holds a client id, time, vote, comment, question, `context_q`, document scope, mode, answer,
  source ids and titles, and the words that matched nothing. Fields are size-capped. A later line
  with the same id (adding a comment) replaces the earlier one.
- **Review:** `GET /api/feedback` returns the totals plus the latest 500 entries. `GET /feedback`
  is a plain review page: answers marked wrong first, each with its sources linked, a
  "words not found" hint with an `ASK_SYNONYMS` example, and the comment.
- **Privacy:** it stays on the server. With `ASK_PASSWORD` set it's behind the same login, and
  `FEEDBACK=off` disables it.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `EMBED_MODEL` | `all-minilm` | Ollama embedding model; `off` disables search by meaning |
| `EMBED_MAX_CHUNKS` | `40` | Passages embedded per document |
| `SEMANTIC_MIN` / `SEMANTIC_BAND` | per model (nomic 0.45, minilm 0.25) / `0.06` | Similarity floor / how close to the best match a document must be to get the boost |
| `FEEDBACK` | `on` | Thumbs up/down and the review page |

## Calibration (live library, 29 documents, 222 passages, 2026-10-10)

The test used 12 paraphrased questions with known answers (e.g. "grass cutting bill" for a lawn
service invoice) and 8 questions about things the library doesn't contain (passport,
mortgage, gym membership, …).

| Model | Index time | Query time | Right document first | Real matches | Unrelated questions |
|---|---|---|---|---|---|
| all-minilm | 10 s | ~16 ms | 9 / 12 | 0.21–0.59 | 0.21–0.53 |
| nomic-embed-text | 67 s | ~40 ms | 11 / 12 | 0.48–0.67 | 0.50–0.64 |

With both models the two ranges overlap, so no threshold separates "found" from "not there".
For example, "my passport renewal" scored 0.64 against a tax letter. That's why meaning only
re-ranks and never overrules a "couldn't find". nomic-embed-text is the default because it
ranks better. The deployment needs `OLLAMA_MAX_LOADED_MODELS` ≥ 2; Ollama's default is 3.

## Live check (2026-10-10)

The probes ran on a temporary instance against the real library. They found:
- **Instant answers ignored follow-ups.** "and last year's?" and "what about the other car?" got
  the first question's instant answer again. Now a follow-up gets an instant answer only when its
  own words ask for a fact, and questions about an older version ("last year's", "previous") always
  go to the model.
- **An instant answer came from the wrong subject.** "When does my passport expire?" was answered
  with a car lease's expiry, because the lease mentions a passport. Now an instant answer needs a
  card that's about the question's subject (vendor, kind, category, summary, title or ID labels,
  plus synonyms).
- **Search by meaning outranked recency.** Its boost put a 2013 insurance card above the current
  one, over similarity differences of 0.02. Now every document in the band gets the same boost,
  and word matches and recency order them.
- **"per" counted as a word found nowhere** ("cost per month"). It's now a stop word.

After the fixes:
- the follow-up chain works;
- "grass cutting bill" gives an honest "couldn't find", with the lawn invoice first by meaning;
- "What is the minimum payment?" scoped to a card notice gives $280.00 (the notice's balance is
  $2,469.64);
- feedback and the review page work.

Known miss: "exercise equipment" doesn't find the NordicTrack invoice. That was also the miss in
calibration.

## Testing

- **Units:** follow-up detection and query merging (including date replacement); scoped
  retrieval; the embedding store (round trip, staleness, pruning) and cosine ranking with fake
  vectors; feedback append, replace and review rendering.
- **App (fake Paperless + fake Ollama with a deterministic `/api/embed`):**
  - a word-free match found by meaning instead of being refused;
  - a missing embedding model turns the feature off cleanly;
  - follow-up prompts carry the context, and `context_q` chains;
  - scoped questions use only that document;
  - feedback round trip and the review page;
  - the `off` switches.
- **Live, before release:** calibration questions, the follow-up examples from the request, and a
  scoped question on a real document.
