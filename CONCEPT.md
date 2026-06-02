# SEC Notice Agent — Concept

An agentic monitoring system that **watches** the SEC EDGAR archive for new
filings, has a **Claude agent analyse** each one and assign a **materiality /
alert score**, and **emails** a notification when something matters.

> Status: design doc. No code yet — this is the agreed concept before scaffolding.

---

## 1. Goal & scope

- **Watch** one or more companies (by CIK) for newly published filings.
- **Analyse** each new filing with a competent AI agent (Claude Agent SDK,
  Python). The agent's job is to read the filing, use the tools provided to it,
  and produce a **materiality assessment**: how important/urgent is this filing,
  and why.
- **Notify** by **email** when a filing crosses an alert threshold.
- Everything (filing + analysis + notification record) is persisted so we have
  history and can avoid re-alerting on the same thing.

Initial scope decision is open (MSTR-only vs watchlist vs any-CIK) — the design
below works for all three; we just seed the watchlist differently.

---

## 2. Why EDGAR is easy to automate

The archive has a strict, predictable layout:

```
https://www.sec.gov/Archives/edgar/data/<CIK>/<ACCESSION>/<DOCUMENT>
        e.g.        .../edgar/data/1050446/000119312526249768/mstr-20260530.htm
                                   │        │                  │
                                   CIK      one filing         one document in it
                                  (company) (accession no.)    (.htm / exhibit / xml)
```

- **CIK** = a company (1050446 = MicroStrategy / Strategy).
- **Accession number** = one filing submission (a 10-K, 8-K, etc.).
- A filing bundles several documents (primary report, exhibits, an XBRL data file).

We do **not** scrape HTML to find filings. SEC publishes structured endpoints
(free, no API key — just a descriptive `User-Agent` header and ≤10 requests/sec):

| Purpose | Endpoint |
|---|---|
| All filings for a company | `https://data.sec.gov/submissions/CIK##########.json` (10-digit, zero-padded) |
| Documents in one filing | `https://www.sec.gov/Archives/edgar/data/<CIK>/<ACCESSION>/index.json` |
| New-filing feed (poll) | per-company Atom: `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=<CIK>&type=&output=atom` |
| Full-text search (optional) | `https://efts.sec.gov/LATEST/search-index?q=...` |

Compliance: set `User-Agent: "SEC Notice Agent (hpai.bantwal@gmail.com)"`,
throttle to ≤10 req/s, and back off on HTTP 429.

---

## 3. Architecture

```
            ┌───────────────────────────────────────────────────────────┐
            │                      Scheduler (cron / loop)               │
            └───────────────────────────────────────────────────────────┘
                                       │ every N minutes, per watched CIK
                                       ▼
   ┌──────────┐     ┌──────────┐     ┌───────────────────────────┐     ┌──────────┐
   │ DISCOVER │────▶│  FETCH   │────▶│      ANALYSE (agent)      │────▶│  NOTIFY  │
   │ new      │ new │ download │ docs│  Claude Agent SDK:        │alert│  email   │
   │ filings  │ acc.│ documents│     │  read + run YOUR tools →  │ ≥   │  if score│
   │ vs DB    │     │          │     │  {score, reasons, summary}│ thr.│  ≥ thresh│
   └──────────┘     └──────────┘     └───────────────────────────┘     └──────────┘
        │                │                        │                          │
        └────────────────┴────────────────────────┴──────────────────────────┘
                                   persist to DB + file store
```

**DISCOVER** — pull the submissions JSON (or Atom feed) for each watched CIK,
diff against what's already in the DB, emit the set of *new* accession numbers.

**FETCH** — for each new filing, read its `index.json`, download the primary
document (+ relevant exhibits/XBRL), store raw files on disk, record paths.

**ANALYSE** — the core. A Claude agent (Agent SDK) receives the filing content
and a toolset (the tools you provide). It produces a structured materiality
verdict (schema in §5). This is where "competent AI agent" lives.

**NOTIFY** — if `score >= threshold` and we haven't alerted on this accession
already, send an email with the summary + reasons + links; record the alert.

---

## 4. Data model

```
companies        cik PK, name, ticker, sic/industry, last_polled_at
filings          id PK, accession_no UNIQUE, cik FK, form_type, filing_date,
                 period_of_report, primary_doc_url, status (new|fetched|analysed|notified)
documents        id PK, filing_id FK, filename, doc_type, size, url, local_path,
                 extracted_text
analyses         id PK, filing_id FK, model, materiality_score, severity,
                 summary, reasons (json), tool_calls (json), tokens, created_at
notifications    id PK, filing_id FK, channel (email), recipient, subject,
                 status (sent|failed), sent_at
```

`filings.status` makes the pipeline **idempotent and resumable** — each stage
advances the status, so a crash/restart re-processes only unfinished filings and
we never double-email on an accession that's already `notified`.

Storage: **SQLite** to start (zero-setup, single file). Trivial to migrate to
Postgres later if the watchlist or query needs grow.

---

## 5. The agent (Claude Agent SDK, Python)

The agent is given the filing text + metadata and instructed to assess
materiality. It can call the tools you provide; the run ends when it emits a
structured result via a `submit_assessment` tool (forces clean JSON output).

**Materiality output schema:**

```jsonc
{
  "materiality_score": 0,        // 0–100, how market-moving / urgent
  "severity": "info",            // info | notable | high | critical
  "summary": "one-paragraph TL;DR of the filing",
  "reasons": [                   // why this score — drives the email body
    "8-K Item 1.01: entered a $X material definitive agreement",
    "Discloses additional bitcoin purchase of N BTC"
  ],
  "tags": ["material-agreement", "crypto-holdings"],
  "recommended_action": "notify" // notify | watch | ignore
}
```

**Why Agent SDK over a hand-rolled loop:** native tool-calling loop, retries,
and message management are handled for us; plugging in tools is a registration
call rather than wiring a custom dispatch loop. Uses the latest Claude model
(e.g. Opus/Sonnet 4.x) with prompt caching on the system prompt + filing text
to keep cost down.

---

## 5.1 Cross-check sources (impact analysis)

A filing read in isolation only tells us what the company *says*. Materiality is
triangulated across three lenses:

| Lens | Question | Source |
|---|---|---|
| **Stated** | What does the filing claim? | the filing (ingested) |
| **Relative** | Is this a change vs history / scale? | prior filings, fundamentals |
| **Realized** | Did the market actually react? | price/volume, news, on-chain |

**Tool roster the agent gets (Phase 3):**

| Tool | Purpose | Backing source | Dependency |
|---|---|---|---|
| `get_filing_text` | full / section text of current filing | our DB | — |
| `get_prior_filings` | "is this a change?" context | our DB | — |
| `get_insider_activity` | Form 4 buys/sells in a window | our DB | — |
| `get_company_facts` | scale a number vs revenue / market cap | SEC XBRL `companyfacts` API | free |
| `get_price_reaction` | filing-day + next-day return vs 30-day avg volume | `yfinance` | free (key-less) |
| `search_news` | corroboration, analyst takes, narrative | built-in **WebSearch** | already here |
| `get_btc_context` | BTC price + on-chain context (for crypto-linked issuers like MSTR) | **Dune MCP** | already connected |
| `submit_assessment` | emit the structured verdict (forces clean JSON) | — | — |

Design guards baked into the tools:
- **De-confound** — `get_price_reaction` also returns a sector/market baseline so
  a macro-wide move isn't mis-credited to the filing.
- **No look-ahead** — every external tool takes the filing date; for backfilled
  filings it only uses data available *as of* that date.
- **Cite-or-omit** — any figure in the verdict must come from a tool result;
  high-score findings get an adversarial verify pass before notifying.
- **Cost control** — only forms above a materiality floor (8-K / 10-K / 10-Q by
  default) get the full external cross-check; routine forms (Form 4, etc.) are
  scored cheaply or skipped.

Agent loop: *read filing → quick stated score → if material form & above floor,
pull reaction + fundamentals + news (+ BTC for crypto issuers) → reconcile into a
final cited score → verify → hand to NOTIFY.*

---

## 6. Notification (email)

- Trigger: `recommended_action == "notify"` **and** `materiality_score >=
  THRESHOLD` (configurable, default ~60) **and** no prior `notified` record for
  this accession.
- Content: subject like `[HIGH] MSTR 8-K — material agreement (score 78)`;
  body = summary + bullet reasons + direct link to the filing on EDGAR.
- Delivery: start with **SMTP** (Gmail app password) or an API like SES/Resend —
  decision deferred; the `notifications` table abstracts the channel so we can
  add Slack/Telegram later without touching the pipeline.

---

## 7. Scheduling / watch loop

- A scheduler runs DISCOVER→…→NOTIFY for each watched CIK every N minutes.
- Options: a cron job, a long-running poller, or a managed scheduled run.
- Respect EDGAR rate limits; jitter polls; honor 429 backoff.
- De-dup is guaranteed by `accession_no UNIQUE` + `filings.status`, so frequent
  polling is safe.

---

## 8. Tech stack

- **Language:** Python 3.11+
- **Agent:** Claude Agent SDK
- **HTTP:** `httpx` (with retry/backoff)
- **DB:** SQLite via SQLAlchemy (easy Postgres swap)
- **Parsing:** `selectolax`/`beautifulsoup4` for HTML→text; `lxml` for XBRL later
- **Email:** `smtplib`/SES/Resend (TBD)
- **Config:** `.env` (CIK watchlist, threshold, recipient, API keys, User-Agent)

```
sec-notice-agent/
├── CONCEPT.md
├── pyproject.toml
├── .env.example
├── src/sec_notice/
│   ├── edgar/        # discover.py, fetch.py, client.py (rate-limited http)
│   ├── agent/        # analyst.py (Agent SDK), tools.py, schema.py
│   ├── store/        # models.py, db.py
│   ├── notify/       # email.py
│   ├── pipeline.py   # discover→fetch→analyse→notify orchestration
│   └── watch.py      # scheduler entrypoint
└── tests/
```

---

## 9. Build phases

1. **Ingest one filing** — hardcode the pasted MSTR URL; fetch → store doc +
   text in DB. Proves FETCH + storage.
2. **Discover a company** — submissions JSON → backfill all MSTR filings (no
   analysis yet). Proves DISCOVER + de-dup.
3. **Analyse** — wire the Claude Agent SDK with `submit_assessment` + the two
   built-in tools; write `analyses` rows. Plug in your tools here.
4. **Notify** — email on threshold; record `notifications`; guarantee no dupes.
5. **Watch** — schedule the full loop; add the rest of the watchlist.

Each phase is independently runnable and leaves a working artifact.

---

## 10. Open questions (to resolve before/while coding)

- **Email transport:** Gmail SMTP app-password vs SES/Resend?
- **Watchlist source:** MSTR-only, a fixed list, or any-CIK-on-demand?
- **Form filter:** all forms, or only material ones (8-K, 10-K, 10-Q, 4, S-1…)?
- **Your tools:** what tools will the agent get, and what do they return? (shapes
  the agent prompt + schema).
- **Threshold + severity mapping:** what score = email vs silent record?
- **Schedule:** poll interval and where it runs (cron / daemon / managed).
```
