# Design: fact cards, "coming up" reminders, and totals

Status: accepted, 2026-10-10. Target release: 1.1.0.

## Goals

1. **Coming up**: warn before documents' expiry and due dates (an insurance card that expires next
   week, a bill due Friday) without anyone asking. Any install can consume them: an iCal feed for
   phone and desktop calendars, a JSON endpoint, and a documented Home Assistant recipe for push
   notifications.
2. **Fact cards**: extract each document's key facts once (kind, category, vendor, total, labelled
   dates, ID numbers) with the configured Ollama model, in the background, and persist them. Simple
   questions ("what's my policy number?") are then answered instantly from the card.
3. **Totals**: "How much did I spend on medical in 2025?" is answered by adding up the fact cards'
   totals in code (never by the model), with an itemised, cited list.

Non-goals: writing anything back to Paperless; multi-currency conversion; summarising the whole
library; extraction for libraries far beyond a few thousand documents.

## Principles carried over from 1.0

- Nothing the model says is trusted unless it can be found in the document text. Every extracted
  amount, date and ID is checked against the OCR text, and anything not found there is dropped.
- The app stays useful without the model. Reminders also come from a pattern-based date finder,
  so they work with `FACTS=off`.
- Questions come first. Extraction runs only when no question is waiting, one document at a time,
  and optionally only inside quiet hours (`FACTS_HOURS`), because Ollama is often shared.

## Components

| Module | Responsibility | Depends on |
|---|---|---|
| `facts.py` | Prompt + JSON schema, grounding (dropping ungrounded values), `FactStore` (JSON file in `DATA_DIR`, atomic writes, invalidated by document `modified` or extractor version) | `textutil` helpers |
| `reminders.py` | Pattern-based labelled dates, merging with fact-card dates, superseded/paid filtering, `upcoming()`, iCal rendering | fact cards, documents |
| `totals.py` | Detecting total questions, filtering by category/vendor/period, de-duplicating, summing | fact cards, documents |
| `app.py` | Wiring: background worker, endpoints, fact-card fast answers, UI config | all of the above |

Shared text helpers (date/number variants, money regex) move from `app.py` to `textutil.py`, so the
new modules don't import the web app.

## Fact cards

Schema (enforced with Ollama structured output): `kind` (receipt, invoice, bill, statement,
insurance, tax, medical, pay, contract, id, letter, other), `category` (medical, insurance,
utilities, shopping, home, vehicle, tax, income, travel, food, finance, other), `vendor`, `summary`,
`total`, `dates[] {label: due|expires|issued|paid|service|effective|other, date: YYYY-MM-DD}`,
`ids[] {label, value}`.

Grounding rules:
- `total`: must parse as a number, and a variant of it ("1,099.00", "1099", "$1,099") must appear in the text.
- `dates`: must be valid, and one of their written forms (10/22/2026, Oct 22, 2026, …) must appear in the text.
- `ids`: must contain a digit, have at least 4 characters, and appear in the document body, ignoring spaces,
  dashes and case. The title doesn't count, because it's often a scanner file name like `MX-M264N_…`.
  Surrounding punctuation is stripped.
- `vendor`: kept only if one of its words (3+ letters) appears in the text. Otherwise the Paperless correspondent is used.
- Unknown `kind`/`category` values become `other`.

Storage: `DATA_DIR/facts.json`, keyed by document id. Each card stores the document's `modified`
time, the model, the extractor version and the time of extraction. A card is redone when the
document changes or the extractor version changes, and removed when the document disappears.

Worker: checks once a minute. It extracts the newest document still missing a card when all of
these hold:
- facts are enabled;
- the time is inside `FACTS_HOURS` (if set);
- Ollama is reachable;
- no question has been asked in the last 2 minutes;
- the LLM lock is free.

If Ollama fails, the worker backs off for 10 minutes. Text is capped at `FACTS_MAX_CHARS` (default 4000).

## Coming up

Dates come from two sources, merged and de-duplicated per (document, label, date):
- **Patterns:** "Expiration Date: 10/22/2026", "Expires 10/22/26", "Payment due date …", "Due by …",
  "Pay by …", "Renewal date …". These need no model.
- **Fact cards:** dates labelled `due` or `expires`.

Filtering:
- Only today and later, within the requested window (default `REMINDER_DAYS=60`).
- A `due` date is skipped when the document has a tag listed in `DONE_TAGS` (default `paid`).
- An `expires` date is skipped when a newer document from the same vendor/correspondent, of the
  same kind, has a later expiry date (the renewed card).

Endpoints:
- `GET /api/upcoming?days=N` returns `{items: [...], alerts: [...]}`. Each item has the document id,
  title, label, date, `days_left`, vendor, amount, url and a ready-made `text`. `alerts` holds the
  items whose `days_left` is in `ALERT_DAYS` (default `14,3,1,0`).
- `GET /calendar.ics` returns all-day events for the next 365 days, with alarms at each `ALERT_DAYS`
  value. The feed has stable UIDs, so calendar apps update it in place. Calendar apps send the
  Basic-auth login when `ASK_PASSWORD` is set.

UI: a "Coming up" list under the ask box on the start page, plus a "Subscribe in your calendar" link.

## Instant answers from fact cards

After retrieval, if the top document has a card and the question asks for exactly one kind of fact,
the app answers from the card (mode `facts`) without calling the model. The question types are:
- expiry ("when does … expire");
- due date;
- a number ("policy number", "account number", …, matched against the ID label);
- an amount ("how much", "cost", "total").

Candidates are collected from every document the model would have read. For date questions, past
dates are dropped when there's more than one candidate: last decade's insurance cards are history.
If there are zero or several candidate values, the normal model path runs instead. The answer
card offers "Ask the AI instead", which re-asks with `full=1`. The JSON API accepts `"full": true`.

## Totals

Triggered by questions like "how much did I spend / pay …", "total spent on …" or "what did … cost
in total".

Filters:
- **Period:** year/month from the question, matched against the card's paid/issued/service date,
  falling back to the document's created date.
- **Category:** words like medical, doctor, utilities, electric, insurance, car, … mapped to card
  categories.
- **Vendor:** any other content word that matches a card's vendor or the document title.

Items included: cards with a total whose kind isn't insurance, id, tax or pay. A card whose kind is
`medical` counts as medical spending whatever category the model chose.

Two items are treated as the same charge, so the second is listed as excluded, when all of these hold:
- same amount;
- same vendor, ignoring punctuation;
- within 60 days of each other;
- either different kinds of document (a receipt and a statement) or a shared ID number.

Two receipts with different order numbers are two purchases.

Response mode `total`: the sum, an itemised list (each item cited and linked), the excluded items
with reasons, and how many matching documents still lack a card. That last count is shown as
"N documents not analysed yet".

## Configuration (all optional)

| Variable | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `$STATE_DIRECTORY`, else `./data` (Docker: `/data`) | Where `facts.json` lives |
| `FACTS` | `on` | `off` disables extraction, instant answers and totals |
| `FACTS_HOURS` | any time | e.g. `1-6`: only extract between 01:00 and 06:59 local time |
| `FACTS_MAX_CHARS` | `4000` | Text sent per document |
| `REMINDER_DAYS` | `60` | Default window for `/api/upcoming` and the start page |
| `ALERT_DAYS` | `14,3,1,0` | Days-before values that raise alerts and calendar alarms |
| `DONE_TAGS` | `paid` | Tags (comma-separated) that mean a due date is handled |

## Live check (2026-10-10, 29 real documents)

First pass: 14–69 s per document, about 25 minutes in total. Changes made after reviewing the cards:
- past-date disambiguation (three insurance cards with expiry dates);
- medical-kind counting;
- the duplicate rule above (a punctuation difference in a vendor name, and two genuine same-price
  receipts);
- dropping IDs taken from titles;
- not counting documents without text as "not analysed yet".

Known limit: for credit-card statements, the model's "total" is the statement balance, so a due-date
reminder shows the balance rather than the minimum payment.

## Testing

- Unit tests: grounding (each rule, with the failure cases seen in the spike: names as IDs, `"null"`
  totals, two due dates, ungrounded vendors); pattern dates; superseded/paid filtering; iCal output;
  total detection, filtering, de-duplication and the sum.
- App tests (fake Paperless + fake Ollama with a structured-output response): the worker fills the
  store, the store survives a restart, a changed document is re-extracted, instant answers, totals
  mode, `/api/upcoming`, `/calendar.ics`, and the `FACTS=off` behaviour.
- Live check on CT 108 before release: a full extraction run, the cards reviewed by hand against
  the documents, and the reminders compared with the known dates.
