# Finance & Interview Log — Design

Date: 2026-08-12
Status: Approved by Kai, ready for implementation planning

## Purpose

Kai wants Waku to track two personal logs conversationally:

1. **Daily investment P&L** across his real accounts: A股, 支付宝基金, 雪球基金, IBKR
   (美股), BIT (美股/加密). He reports each day's profit/loss by voice/chat — no
   running balances, no auto currency conversion.
2. **Interview tracking**: company, role, round, date, status (进行中/通过/失败/待跟进),
   and free-text notes/recap (what was asked, how it went).

Both are structured logs, not free-text memory — Kai wants to query them later
("this month's fund P&L?", "which interviews are pending follow-up?") and see
them summarized on the dashboard. This does not fit `save_note` (unstructured)
or `create_event` (calendar semantics), so it follows the same pattern as
`create_event`: dedicated tools writing to dedicated tables in `state.db`.

## Data model

Two new SQLite tables in `.waku/state.db`, following the existing schema
conventions in `waku/tools/` (see how `create_event` defines/migrates its table).

### `finance_entries`

| column | type | notes |
|---|---|---|
| id | integer PK | autoincrement |
| date | text (ISO date) | defaults to today if not specified |
| account | text | one of the fixed enum below |
| currency | text | derived from account, not asked from user |
| pnl_amount | real | signed; positive = profit, negative = loss |
| note | text, nullable | optional free text |
| created_at | text | timestamp of the chat turn that logged it |

Fixed account enum (currency in parentheses):
- `A股` (CNY)
- `支付宝基金` (CNY)
- `雪球基金` (CNY)
- `IBKR` (USD)
- `BIT` (USD)

No auto FX conversion. Aggregation/queries group by currency, never sum CNY +
USD into one number.

### `interview_entries`

| column | type | notes |
|---|---|---|
| id | integer PK | autoincrement |
| company | text | |
| role | text | |
| round | text, nullable | e.g. "一面" / "HR面" |
| date | text (ISO date) | |
| status | text | one of: `进行中`, `通过`, `失败`, `待跟进` |
| notes | text, nullable | recap: questions asked, self-assessment, etc. |
| created_at | text | |
| updated_at | text | bumped whenever status/notes change |

Interview entries are mutable (status changes over time as rounds progress);
finance entries are append-only.

## Tools

Two new tools registered in `waku/tools/`, following the existing
`create_event` tool's shape (schema, safe execution, deterministic eval —
per the `new-tool` skill checklist):

- **`log_pnl`** — params: account (enum), pnl_amount, date (optional,
  defaults today), note (optional). Rejects unknown account names rather
  than guessing.
- **`log_interview`** — params: company, role, round (optional), date
  (optional, defaults today), status, notes (optional). If an entry for the
  same company+role+round already exists and is still open, this updates it
  (status/notes) rather than creating a duplicate — avoids fragmenting one
  interview process across multiple rows as it progresses through rounds.

No new query tools are added. Waku answers questions like "this month's fund
P&L?" by reading these tables directly the same way `list_events` already
reads the calendar table — the LLM composes the SQL-backed read from natural
language, no bespoke aggregation tool needed for v1.

## Dashboard

One new tab: **Finance**, added to the existing dashboard tab set
(`waku/ops/static/`), following the visual/structural pattern of the current
Memory/Loop tabs:

- Per-account table: date, pnl_amount, note, most recent first.
- Simple per-account and per-currency running totals (CNY accounts summed
  separately from USD accounts).
- An **Interviews** section on the same tab (or a small sub-tab) listing
  entries grouped by status, most recently updated first.

No charts/trend lines in v1 — table + totals is enough to start. Charting can
be a follow-up once real data exists to shape it around.

## Out of scope (explicitly deferred)

- CSV/statement import and any brokerage/exchange API integration.
- Auto FX conversion / unified net-worth number.
- Reminders or follow-up nudges for interviews (e.g. "3 days since last
  contact") — noted as a possible future addition, not built now.
- Editing/deleting past finance entries via chat (append-only for now; fixes
  go through the dashboard's existing SQL console if ever needed).

## Testing

Per `CLAUDE.md`'s gate requirement: deterministic evals under
`evals/deterministic/` for both tools — valid entry, unknown account
rejection (`log_pnl`), and the update-vs-create branch for `log_interview`.
