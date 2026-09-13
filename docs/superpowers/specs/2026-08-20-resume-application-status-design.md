# Resume Application Status — Design

Date: 2026-08-20
Status: Approved by Kai, ready for implementation planning

## Purpose

Kai wants to track resume submissions ("投简历的历史") — companies/roles he's
applied to, before any interview round has happened. This is the earliest
stage of the same pipeline `log_interview` / `interview_entries` already
track (进行中/通过/失败/待跟进), so it extends that existing log rather than
introducing a parallel table. See the prior design at
`docs/superpowers/specs/2026-08-12-finance-and-interview-log-design.md`.

## Data model changes

`interview_entries` (existing table, `waku/db.py`):

- New column `channel TEXT DEFAULT ''` — how the application was submitted
  (e.g. 官网/猎聘/内推/LinkedIn). Additive, idempotent migration in
  `_migrate()`, same pattern as the existing `session_id`/`source`/`meta`
  upgrades for `chat_log`.
- New status value `已投递` (applied, no interview yet), added to both:
  - `VALID_STATUSES` — now `已投递, 进行中, 通过, 失败, 待跟进`
  - `OPEN_STATUSES` — now includes `已投递`, so a later `log_interview` call
    for the same company (e.g. status `进行中` once a round is scheduled)
    updates the existing `已投递` row in place instead of creating a second
    row for the same process.

No new table, no new tool — `log_interview` already treats one company as
one process to be updated across statuses; `已投递` is just the earliest
status in that same sequence.

## Tool changes (`waku/tools/interviews.py`)

- `log_interview` gets a new optional parameter `channel: str | None = None`,
  following the exact "omitted/empty string does not overwrite" semantics
  already implemented for `round` and `notes`.
- Tool description updated to mention `已投递` as the status to use when a
  resume has just been submitted and no interview has happened yet.

## Dashboard (`waku/ops/static/js/views.js`)

- `STATUS_ORDER` gains `已投递`, placed first (earliest pipeline stage).
- The interviews table gains a Channel column.

## Out of scope

- No new query/reporting tool — same as the original design, Waku answers
  aggregate questions ("投了多少家还没回复？") by reading the table directly.
- No reminders/follow-up nudges (already deferred in the original design).

## Testing

Deterministic evals added to `evals/deterministic/test_interview_tool.py`:

- Creating a row with status `已投递` stores `channel` correctly.
- A later call with status `进行中` for the same company updates the same
  `已投递` row (reuses the existing open-status update path).
- Omitting `channel` on a later call preserves the existing value (same
  pattern as the existing round/notes omission tests).
