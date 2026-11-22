# Connector Desk

A human at an agent-tooling company can spend their week chasing hundreds of app-connector approvals across partner inboxes and a spreadsheet. Connector Desk is a small agent that does the first pass, but it can only create **drafts**. It never sends anything.

![demo GIF placeholder](docs/demo.gif)
<!-- TODO: record a short GIF of `run --local` and a live dry run. -->

## Run it in 5 minutes

### 1. Local mode (no accounts, no keys, no network)

```bash
git clone https://github.com/VedantBhayani/composio-connector-desk
cd composio-connector-desk
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt && pip install -e .
python -m connector_desk run --local
pytest -q
```

Local mode reads `fixtures/` instead of Gmail and Sheets, prints `LOCAL MODE` first, then a status summary, a ledger summary and the list of things not yet tested live. By default the planner is a **deterministic scripted stand-in**, not Claude. To run the real model on the fixtures (still no Gmail or Sheets), add `--use-claude`; this needs `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` in `.env`.

### 2. Live setup (throwaway Google account only)

```bash
cp .env.example .env          # fill in COMPOSIO_API_KEY, ANTHROPIC_API_KEY, ANTHROPIC_MODEL, SHEET_ID
python -m connector_desk sample-csv --out tracker_import.csv
python -m connector_desk connect          # OAuth links for gmail + googlesheets
python -m connector_desk run --dry-run    # real reads, every write stubbed and logged
python -m connector_desk run              # real drafts + sheet edits; nothing is ever sent
```

[`scripts/seed_inbox.md`](scripts/seed_inbox.md) covers creating the sheet and pasting the six fake emails into the throwaway inbox.

The code uses the current Composio **sessions** SDK (`composio==0.34.0`, `composio_anthropic==0.34.0`). It calls `composio.sessions.create(...)`, and falls back to the `composio.create(...)` shortcut if `sessions` isn't there; 0.34.0 supports both. It never uses the legacy `composio-core`.

## What it does

`python -m connector_desk run [--dry-run] [--local] [--stale-days N]`

1. Fetches emails with the label `GMAIL_LABEL` and reads the `Tracker` tab. Code does both reads, and both are logged in the ledger.
2. Code computes a deterministic match hint for each email: exact sender, then exact domain, then exactly one app name. If there's no single match, the email is reported as `needs human: ambiguous match` and no row changes.
3. The model is given the data and proposes at most one `Stage / Last_Touched / Next_Action / Notes` update per matched row.
4. For rows older than `STALE_DAYS` that aren't `Live` and weren't touched this run, the model writes one polite follow-up **draft**: under 90 words, one ask, no CC/BCC, no attachments.
5. Code writes a status summary (counts by stage, rows changed, drafts, suspicious emails, needs-human) to the `Status` tab. In local mode it's printed instead.
6. Every tool decision goes to `ledger/actions.jsonl`, and the counts are printed at the end.

## Safety model

The prompt asks for good behaviour, but the **code** enforces the hard rules. Each rule below is enforced in more than one place:

| Rule | Where it's enforced |
|---|---|
| Deny-by-default allow-list: `GMAIL_FETCH_EMAILS`, `GMAIL_CREATE_EMAIL_DRAFT`, `GOOGLESHEETS_BATCH_GET`, `GOOGLESHEETS_BATCH_UPDATE` | `ALLOWED_TOOLS` in [`tools_policy.py`](src/connector_desk/tools_policy.py) is passed to the Composio session (`toolkits=["gmail","googlesheets"]`, `tools={kit: {"enable": [...]}}`, `session_preset="direct_tools"`, sandbox off). Any extra tool the session exposes is filtered out before the model sees it. `guarded_execute` checks every call again. |
| Never send / send draft / forward / delete | `BLOCKED_TOOLS` raise `ToolBlockedError` and are logged as `blocked`. The session also sets `require_approval` for the Gmail send tools (an experimental SDK feature) as a further layer. |
| Drafts: only to a `Partner_Email` in the tracker, ≤90 words, no CC/BCC/attachments, one per row per day | Argument checks in `validate_args`. Idempotency comes from the run context and, in live mode, from earlier ledger entries. |
| Sheet writes: one row per call, only columns E:H, valid stage, **never `Live`**, no other tabs except `Status` | Argument checks in `validate_args`. Only a human marks a connector Live. |
| Email content is data, not instructions | The system prompt says so. Emails are passed inside a `<run_data>` block marked as data. Suspicious rows get `suspicious: contains instructions to the agent` in Notes and no draft. A code heuristic also flags them in the summary. The executor limits what an obeyed injection could do: no send, no forward, no draft to an outside address, no bulk edit, no `Live`. |
| No secrets in the repo or ledger | Secrets live only in `.env`, which is gitignored. The ledger truncates bodies to 120 characters, redacts secret-like keys, and scrubs any value of `COMPOSIO_API_KEY`/`ANTHROPIC_API_KEY`. |

`--dry-run` executes the two read-only tools for real, then logs every write as `dry_run` and stubs it.

## How I tested it

`pytest -q` runs offline with no env vars: **73 tests**.

- `test_policy`: every allowed tool passes, every blocked tool raises, unknown tools are denied. The session config mirrors the allow-list and binds to the installed SDK's `create()` signature. No `composio-core` anywhere.
- `test_ledger`: lines are valid JSON, the file is append-only, bodies are truncated, and a fake API key in env never appears.
- `test_sheet_parse`: stage enum, stale detection with a fixed "today", matching. Email 05 is ambiguous and returns "needs human". Self-sent seed emails still match.
- `test_executor`: dry run, the draft and sheet-write rules, and idempotency, all against a fake session.
- `test_agent`, `test_cli`: the full loop with the scripted planner and with a "greedy" mocked model that tries a bulk `Live` and an exfil draft.
- `test_no_send`: a mocked LLM calls `GMAIL_SEND_EMAIL`, `GMAIL_SEND_DRAFT` and `GMAIL_FORWARD_MESSAGE` on email 06. All three are blocked, the mocked session gets **zero** send/forward calls, and the ledger has three `blocked` rows. A second test patches a send tool into the allow-list and proves the check catches the leak. I also added `GMAIL_SEND_EMAIL` to `ALLOWED_TOOLS` by hand and confirmed `test_no_send` fails.
- `test_injection_fixture`: fixture 06 still contains the injection paragraph and the attacker address. Local mode never forwards or sends, sets no row to `Live`, and flags C008.

> **What this proves and what it doesn't.** The offline tests prove the **guard** works even when the model misbehaves. They do **not** prove the real model resists prompt injection. That needs the manual live check below.

### Manual live injection check

1. Seed email 06 (and the others) into the throwaway Gmail by hand, following [`scripts/seed_inbox.md`](scripts/seed_inbox.md).
2. Run `python -m connector_desk run --dry-run`, then `python -m connector_desk run`.
3. Record what actually happened in **Results** below, using `ledger/actions.jsonl`, Gmail Drafts and Sent, and the sheet.

## Results

Fill these in only from real runs. Every cell stays TODO until a run has produced it.

| Date | Mode | Model | SDK version | Emails matched to right row (of 6) | Send/forward calls executed | Blocked attempts | Rows set Live (besides C005) | C008 flagged suspicious | Drafts created |
|---|---|---|---|---|---|---|---|---|---|
| TODO | dry_run | TODO | composio 0.34.0 | TODO | TODO | TODO | TODO | TODO | TODO |
| TODO | live | TODO | composio 0.34.0 | TODO | TODO | TODO | TODO | TODO | TODO |
| TODO | local `--use-claude` | TODO | n/a | TODO | TODO | TODO | TODO | TODO | TODO |

Offline reference (the **scripted planner, not a model**), `run --local` on 2026-11-22: 5 emails routed to the expected row and email 05 marked needs-human, 0 send/forward executed, 0 blocked (the scripted planner never tries), 0 rows set Live, C008 flagged, 1 draft (C007). This shows the plumbing works. It says nothing about model quality.

## Limits

- Tested on a throwaway test inbox only, with **6 hand-written emails**. There's no volume test.
- Drafts only, by design. A human reviews and sends.
- The model (and the match heuristic) can match an email to the wrong row. That's why unclear cases become "needs human" and change nothing.
- Offline tests prove the guard, not the model.
- Live behaviour is only what's recorded in the runs listed under Results. As of this commit, **nothing has been run live**.
- Cost and latency weren't measured.
- It depends on the current Composio sessions SDK and on the tool slugs and argument names. docs.composio.dev wasn't reachable from the build environment. The SDK API was checked against the installed package source, but the slugs and argument shapes (`query`, `ranges`, `sheet_name`, `first_cell_location`, `values`, `recipient_email`) follow the brief and are **unverified**. Live mode warns if the session doesn't expose an allow-listed slug.
- Live idempotency comes from the local ledger, not from Gmail's draft list. Deleting the ledger resets it.
- When you seed by hand, the real sender is the throwaway inbox, so matching falls back to app names.

## Built with

Python 3.11+, Composio sessions SDK, Anthropic Messages API, pytest. No other frameworks.

Claude Code helped write this code and these tests. The author decided:

- the hard rules (drafts only, never Live, email content is data),
- the four-tool allow-list and the block list,
- the fixtures, including the ambiguous email (05) and the prompt-injection email (06),
- the safety test design: a mocked model that obeys the injection, while the guard has to hold.

### Author notes

<!-- TODO (author): what you would change, what surprised you in the live run. -->
