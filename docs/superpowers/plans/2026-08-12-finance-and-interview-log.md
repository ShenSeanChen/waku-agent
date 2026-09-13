# Finance & Interview Log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Kai log daily investment P&L and interview progress conversationally through Waku, and view both on a new Dashboard tab.

**Architecture:** Two new SQLite tables (`finance_entries`, `interview_entries`) in `state.db`, two new tools (`log_pnl`, `log_interview`) that write to them following the `create_event` tool pattern, and one new dashboard tab (`Finance`) that reads both tables via the existing `/api/data` → `VIEWS` pipeline. No new query tool — Waku answers aggregate questions by the model composing SQL-free reasoning over what `log_pnl`/`log_interview` already returned plus future reads; v1 ships the write path and the dashboard read path only (see spec's "out of scope").

**Tech Stack:** Python 3.11, stdlib `sqlite3`, pytest (deterministic evals), plain JS/HTML/CSS dashboard (no build step).

## Global Constraints

- No new dependencies (stdlib + anthropic/openai only) — spec doesn't need any, confirmed.
- No emojis in any UI surface (dashboard, CLI, README).
- Fixed account enum, no auto FX conversion: `A股` (CNY), `支付宝基金` (CNY), `雪球基金` (CNY), `IBKR` (USD), `BIT` (USD).
- Every registered tool ships in every prompt — keep both tools narrow, no query/report tool in v1.
- Follow `.claude/skills/new-tool/SKILL.md`: one file per tool in `waku/tools/`, register in `build_registry`, deterministic eval coverage (offline scripted test + `evals/dataset.jsonl` case) for each.
- Gate before shipping: `make gate` must pass (deterministic evals; judge evals need a key, not required here).
- Work happens on a branch, never directly on `main` (protected).

---

### Task 1: Schema — `finance_entries` and `interview_entries` tables

**Files:**
- Modify: `waku/db.py` (add to `SCHEMA` string, after the `chat_log` table definition)
- Test: `evals/deterministic/test_finance_schema.py`

**Interfaces:**
- Produces: two tables reachable via any `sqlite3.Connection` returned by `waku.db.connect()`:
  - `finance_entries(id, date, account, currency, pnl_amount, note, created_at)`
  - `interview_entries(id, company, role, round, date, status, notes, created_at, updated_at)`

- [ ] **Step 1: Write the failing test**

```python
"""DETERMINISTIC EVAL — the finance/interview tables exist with the right shape."""

from __future__ import annotations

from waku.db import connect


def test_finance_entries_table_has_expected_columns(tmp_path):
    conn = connect(tmp_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(finance_entries)").fetchall()}
    assert cols == {"id", "date", "account", "currency", "pnl_amount", "note", "created_at"}


def test_interview_entries_table_has_expected_columns(tmp_path):
    conn = connect(tmp_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(interview_entries)").fetchall()}
    assert cols == {
        "id", "company", "role", "round", "date", "status", "notes", "created_at", "updated_at",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest evals/deterministic/test_finance_schema.py -v`
Expected: FAIL — `sqlite3.OperationalError: no such table: finance_entries`

- [ ] **Step 3: Add the tables to `waku/db.py`**

Insert into the `SCHEMA` string, right before the closing `"""` (after the
`chat_log` table):

```python
-- Daily investment P&L, one row per report. Append-only: fixes go through
-- the dashboard's SQL console, not a chat-driven edit/delete tool.
CREATE TABLE IF NOT EXISTS finance_entries (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,            -- ISO 8601 date
    account TEXT NOT NULL,         -- one of the fixed account enum (see log_pnl)
    currency TEXT NOT NULL,        -- CNY | USD, derived from account
    pnl_amount REAL NOT NULL,      -- signed: positive = profit, negative = loss
    note TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

-- Interview tracking. Mutable: one row per interview PROCESS (company+role),
-- updated in place as it moves through rounds so the log doesn't fragment.
CREATE TABLE IF NOT EXISTS interview_entries (
    id INTEGER PRIMARY KEY,
    company TEXT NOT NULL,
    role TEXT NOT NULL,
    round TEXT DEFAULT '',
    date TEXT NOT NULL,            -- ISO 8601 date of the most recent update
    status TEXT NOT NULL,          -- 进行中 | 通过 | 失败 | 待跟进
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest evals/deterministic/test_finance_schema.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add waku/db.py evals/deterministic/test_finance_schema.py
git commit -m "feat(db): add finance_entries and interview_entries tables"
```

---

### Task 2: `log_pnl` tool

**Files:**
- Create: `waku/tools/finance.py`
- Modify: `waku/tools/__init__.py` (register in `build_registry`)
- Test: `evals/deterministic/test_finance_tool.py`

**Interfaces:**
- Consumes: `waku.tools.registry.Tool` (dataclass, `waku/tools/registry.py:16`); `sqlite3.Connection` from `waku.db.connect`.
- Produces: `waku.tools.finance.make_tool(conn: sqlite3.Connection) -> Tool` named `"log_pnl"`.
  Also exports `ACCOUNTS: dict[str, str]` (account name → currency) as the
  single source of truth for valid accounts — the dashboard doesn't need it
  (it reads `currency` straight off each stored row), but any future tool
  touching accounts should import this dict rather than redefining the list.

- [ ] **Step 1: Write the failing test**

```python
"""DETERMINISTIC EVAL — log_pnl writes the right row, and rejects unknown accounts."""

from __future__ import annotations

from evals.helpers import ScriptedClient, make_waku, response, text_block, tool_block


def test_log_pnl_writes_row(tmp_path):
    gate = response([text_block('{"retrieve": false, "query": "", "reason": "test"}')])
    turn = [
        response(
            [tool_block("log_pnl", {"account": "IBKR", "pnl_amount": 200, "note": "tech rally"})],
            "tool_use",
        ),
        response([text_block("Logged.")]),
    ]
    app = make_waku(tmp_path / "home", client=ScriptedClient([gate] + turn))
    app.respond("log today's IBKR pnl: +200, tech rally")

    row = app.conn.execute(
        "SELECT account, currency, pnl_amount, note FROM finance_entries"
    ).fetchone()
    assert row["account"] == "IBKR"
    assert row["currency"] == "USD"
    assert row["pnl_amount"] == 200
    assert row["note"] == "tech rally"


def test_log_pnl_rejects_unknown_account(tmp_path):
    from waku.db import connect
    from waku.tools.finance import make_tool

    conn = connect(tmp_path)
    tool = make_tool(conn)
    result = tool.fn(account="Robinhood", pnl_amount=50)

    assert "unknown account" in result.lower()
    assert conn.execute("SELECT COUNT(*) FROM finance_entries").fetchone()[0] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest evals/deterministic/test_finance_tool.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'waku.tools.finance'`

- [ ] **Step 3: Write `waku/tools/finance.py`**

```python
"""log_pnl — records a day's investment profit/loss against a fixed account.

Append-only, currency-aware, no FX conversion (CNY and USD accounts are never
summed together). See docs/superpowers/specs/2026-08-12-finance-and-interview-log-design.md.
"""

from __future__ import annotations

import sqlite3
from datetime import date as date_cls

from waku.tools.registry import Tool

# Fixed account → currency map. Adding an account means editing this dict and
# nothing else the tool touches (the dashboard reads it too, Task 6).
ACCOUNTS: dict[str, str] = {
    "A股": "CNY",
    "支付宝基金": "CNY",
    "雪球基金": "CNY",
    "IBKR": "USD",
    "BIT": "USD",
}


def make_tool(conn: sqlite3.Connection) -> Tool:
    def log_pnl(account: str, pnl_amount: float, date: str = "", note: str = "") -> str:
        if account not in ACCOUNTS:
            valid = ", ".join(ACCOUNTS)
            return f"Error: unknown account '{account}'. Valid accounts: {valid}"
        currency = ACCOUNTS[account]
        entry_date = date or date_cls.today().isoformat()
        conn.execute(
            "INSERT INTO finance_entries (date, account, currency, pnl_amount, note) VALUES (?,?,?,?,?)",
            (entry_date, account, currency, pnl_amount, note),
        )
        conn.commit()
        sign = "+" if pnl_amount >= 0 else ""
        return f"Logged {account} {entry_date}: {sign}{pnl_amount} {currency} (state.db, finance_entries)"

    return Tool(
        name="log_pnl",
        description=(
            "Record a day's profit/loss for one of the user's investment accounts. "
            "Use when the user reports how much they made or lost today in a specific "
            f"account. Valid accounts: {', '.join(ACCOUNTS)}. Never guess an account name "
            "that isn't in that list — ask which one they mean instead."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "account": {
                    "type": "string",
                    "enum": list(ACCOUNTS),
                    "description": "Which account this P&L belongs to",
                },
                "pnl_amount": {
                    "type": "number",
                    "description": "Signed profit/loss for the day, in the account's own currency",
                },
                "date": {
                    "type": "string",
                    "description": "ISO date (YYYY-MM-DD); defaults to today if omitted",
                },
                "note": {"type": "string", "description": "Optional short note, e.g. why"},
            },
            "required": ["account", "pnl_amount"],
        },
        fn=log_pnl,
    )
```

- [ ] **Step 4: Register in `waku/tools/__init__.py`**

Add `finance` to the import line and register the tool next to `notes`:

```python
from waku.tools import calendar, finance, memory_admin, messages, notes, search
```

```python
    registry.register(notes.make_tool(conn))
    registry.register(finance.make_tool(conn))
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest evals/deterministic/test_finance_tool.py -v`
Expected: PASS

- [ ] **Step 6: Add a dataset case for the live tier**

Append to `evals/dataset.jsonl`:

```json
{"id": "log-pnl-basic", "input": "Log today's IBKR profit: +200 dollars, tech rally", "expect_tool": "log_pnl", "expect_in_args": {"account": "IBKR", "pnl_amount": 200}}
```

- [ ] **Step 7: Commit**

```bash
git add waku/tools/finance.py waku/tools/__init__.py evals/deterministic/test_finance_tool.py evals/dataset.jsonl
git commit -m "feat(tools): add log_pnl for daily investment P&L"
```

---

### Task 3: `log_interview` tool

**Files:**
- Create: `waku/tools/interviews.py`
- Modify: `waku/tools/__init__.py` (register)
- Test: `evals/deterministic/test_interview_tool.py`

**Interfaces:**
- Consumes: same `Tool`/`sqlite3.Connection` as Task 2.
- Produces: `waku.tools.interviews.make_tool(conn) -> Tool` named `"log_interview"`.
  Update-vs-insert rule: matches an existing row by `company` + `role`
  (case-insensitive) whose `status` is `进行中` or `待跟进` (i.e. still open);
  if found, updates `round`/`status`/`notes`/`date`/`updated_at` in place.
  Otherwise inserts a new row. A closed row (`通过`/`失败`) is never matched,
  so a later re-application to the same company/role starts a fresh row.

- [ ] **Step 1: Write the failing test**

```python
"""DETERMINISTIC EVAL — log_interview creates a row, then updates the SAME
row as an interview progresses through rounds, instead of fragmenting."""

from __future__ import annotations

from waku.db import connect
from waku.tools.interviews import make_tool


def test_log_interview_creates_row(tmp_path):
    conn = connect(tmp_path)
    tool = make_tool(conn)
    result = tool.fn(company="ByteDance", role="Backend Engineer", round="一面",
                      status="进行中", notes="asked about system design")

    assert "ByteDance" in result
    row = conn.execute("SELECT company, role, round, status, notes FROM interview_entries").fetchone()
    assert row["company"] == "ByteDance"
    assert row["round"] == "一面"
    assert row["status"] == "进行中"


def test_log_interview_updates_open_row_on_next_round(tmp_path):
    conn = connect(tmp_path)
    tool = make_tool(conn)
    tool.fn(company="ByteDance", role="Backend Engineer", round="一面", status="进行中")
    tool.fn(company="ByteDance", role="Backend Engineer", round="二面", status="待跟进",
             notes="waiting to hear back")

    rows = conn.execute("SELECT round, status, notes FROM interview_entries").fetchall()
    assert len(rows) == 1  # updated in place, not a second row
    assert rows[0]["round"] == "二面"
    assert rows[0]["status"] == "待跟进"
    assert rows[0]["notes"] == "waiting to hear back"


def test_log_interview_starts_fresh_row_after_closed(tmp_path):
    conn = connect(tmp_path)
    tool = make_tool(conn)
    tool.fn(company="ByteDance", role="Backend Engineer", round="HR面", status="失败")
    tool.fn(company="ByteDance", role="Backend Engineer", round="一面", status="进行中")

    rows = conn.execute("SELECT status FROM interview_entries ORDER BY id").fetchall()
    assert [r["status"] for r in rows] == ["失败", "进行中"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest evals/deterministic/test_interview_tool.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'waku.tools.interviews'`

- [ ] **Step 3: Write `waku/tools/interviews.py`**

```python
"""log_interview — records/updates one interview process (company + role) as
it moves through rounds, so one process stays one row instead of fragmenting.

See docs/superpowers/specs/2026-08-12-finance-and-interview-log-design.md.
"""

from __future__ import annotations

import sqlite3
from datetime import date as date_cls

from waku.tools.registry import Tool

OPEN_STATUSES = ("进行中", "待跟进")
VALID_STATUSES = ("进行中", "通过", "失败", "待跟进")


def make_tool(conn: sqlite3.Connection) -> Tool:
    def log_interview(
        company: str,
        role: str,
        status: str,
        round: str = "",
        date: str = "",
        notes: str = "",
    ) -> str:
        if status not in VALID_STATUSES:
            return f"Error: unknown status '{status}'. Valid statuses: {', '.join(VALID_STATUSES)}"
        entry_date = date or date_cls.today().isoformat()
        placeholders = ",".join("?" * len(OPEN_STATUSES))
        existing = conn.execute(
            f"SELECT id FROM interview_entries WHERE lower(company)=lower(?) AND lower(role)=lower(?) "
            f"AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
            (company, role, *OPEN_STATUSES),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE interview_entries SET round=?, date=?, status=?, notes=?, "
                "updated_at=datetime('now') WHERE id=?",
                (round, entry_date, status, notes, existing["id"]),
            )
            verb = "Updated"
        else:
            conn.execute(
                "INSERT INTO interview_entries (company, role, round, date, status, notes) "
                "VALUES (?,?,?,?,?,?)",
                (company, role, round, entry_date, status, notes),
            )
            verb = "Logged"
        conn.commit()
        return f"{verb} {company} — {role} ({round or 'no round given'}): {status} (state.db, interview_entries)"

    return Tool(
        name="log_interview",
        description=(
            "Record or update an interview. If the same company+role already has an "
            "open entry (进行中 or 待跟进), this UPDATES it in place with the new round/"
            "status/notes instead of creating a duplicate — call it again for each new "
            "round of the same process. Use when the user reports an interview happened, "
            "a result came in, or gives a recap/notes to remember."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company": {"type": "string"},
                "role": {"type": "string"},
                "round": {"type": "string", "description": "e.g. 一面, 二面, HR面"},
                "status": {"type": "string", "enum": list(VALID_STATUSES)},
                "date": {"type": "string", "description": "ISO date; defaults to today"},
                "notes": {"type": "string", "description": "Recap: questions asked, self-assessment, etc."},
            },
            "required": ["company", "role", "status"],
        },
        fn=log_interview,
    )
```

- [ ] **Step 4: Register in `waku/tools/__init__.py`**

```python
from waku.tools import calendar, finance, interviews, memory_admin, messages, notes, search
```

```python
    registry.register(finance.make_tool(conn))
    registry.register(interviews.make_tool(conn))
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest evals/deterministic/test_interview_tool.py -v`
Expected: PASS

- [ ] **Step 6: Add a dataset case**

Append to `evals/dataset.jsonl`:

```json
{"id": "log-interview-basic", "input": "I just had a first-round interview at ByteDance for a Backend Engineer role, went okay, they asked about system design. Log it as in progress.", "expect_tool": "log_interview", "expect_in_args": {"company": "ByteDance", "status": "进行中"}}
```

- [ ] **Step 7: Commit**

```bash
git add waku/tools/interviews.py waku/tools/__init__.py evals/deterministic/test_interview_tool.py evals/dataset.jsonl
git commit -m "feat(tools): add log_interview, update-in-place across rounds"
```

---

### Task 4: Dashboard backend — expose both tables via `/api/data`

**Files:**
- Modify: `waku/ops/dashboard.py` (`collect()`, and the `db_info["tables"]` list)
- Modify: `evals/deterministic/test_dashboard_routes.py` (`test_collect_returns_the_keys_the_page_reads`)

**Interfaces:**
- Consumes: `finance_entries`/`interview_entries` tables from Task 1.
- Produces: `collect()` output gains two keys: `d.finance` (list of finance
  rows, newest first) and `d.interviews` (list of interview rows, most
  recently updated first) — these are the names Task 5's frontend reads.

- [ ] **Step 1: Write the failing test (extend the existing pinned-keys test)**

Modify `test_collect_returns_the_keys_the_page_reads` in
`evals/deterministic/test_dashboard_routes.py`:

```python
def test_collect_returns_the_keys_the_page_reads():
    """`/api/data` is read by every view in static/js/. These are the keys the
    frontend indexes into; dropping one blanks a tab with no error."""
    expected = {
        "settings", "tools", "facts", "episodes", "soul", "chat_log", "sessions",
        "turns", "stats", "db", "skills", "trace_file", "chat_pending", "graph",
        "finance", "interviews",
    }
    src = inspect.getsource(dashboard.collect)
    for key in expected:
        assert f'"{key}"' in src, f"collect() no longer returns: {key}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest evals/deterministic/test_dashboard_routes.py::test_collect_returns_the_keys_the_page_reads -v`
Expected: FAIL — `collect() no longer returns: finance` (it never did)

- [ ] **Step 3: Add the reads to `waku/ops/dashboard.py`**

In `collect()`, next to the existing `"calendar": rows(...)` line (around
line 479), add:

```python
        "calendar": rows('SELECT title, start, "end", attendees, created_at FROM calendar_events ORDER BY start'),
        "finance": rows(
            "SELECT id, date, account, currency, pnl_amount, note, created_at "
            "FROM finance_entries ORDER BY date DESC, id DESC"
        ),
        "interviews": rows(
            "SELECT id, company, role, round, date, status, notes, created_at, updated_at "
            "FROM interview_entries ORDER BY updated_at DESC"
        ),
```

Also add both tables to the Database tab's introspection list (around line
418):

```python
        "tables": [table_info(n) for n in
                   ("calendar_events", "facts", "episodes", "chat_log",
                    "finance_entries", "interview_entries")],
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest evals/deterministic/test_dashboard_routes.py -v`
Expected: PASS (all tests in the file, not just the one — this file is a
characterization net for the whole dashboard surface)

- [ ] **Step 5: Commit**

```bash
git add waku/ops/dashboard.py evals/deterministic/test_dashboard_routes.py
git commit -m "feat(dashboard): expose finance_entries and interview_entries in /api/data"
```

---

### Task 5: Dashboard frontend — the Finance tab

**Files:**
- Modify: `waku/ops/static/index.html` (nav link)
- Modify: `waku/ops/static/js/main.js` (`TITLES` entry)
- Modify: `waku/ops/static/js/views.js` (`VIEWS.finance`)

**Interfaces:**
- Consumes: `d.finance` and `d.interviews` from `collect()` (Task 4) — same
  `d` object every other `VIEWS[...]` function reads, injected as `D` by
  `main.js`'s `refresh()`.
- Produces: a `#finance` nav entry and route, following the exact pattern
  `#memory`/`#database` already use (`data-v="finance"` on the `<a>`,
  `VIEWS.finance` doing the rendering).

No JS test runner exists for this file (per `waku/ops/static/README.md`);
verification is a manual dashboard check, not an automated one — see Step 4.

- [ ] **Step 1: Add the nav link**

In `waku/ops/static/index.html`, add a row after the `Database` link (so it
sits with the other data-bearing tabs, before the `Ops` System-group entry):

```html
  <a href="#database" data-v="database">Database <span class="n" id="n-db"></span></a>
  <a href="#finance" data-v="finance">Finance</a>
  <a href="#ops" data-v="ops">Ops <span class="n" id="n-ops"></span></a>
```

- [ ] **Step 2: Add the page title**

In `waku/ops/static/js/main.js`, add to the `TITLES` map:

```javascript
const TITLES = {chat:"Chat & watch", ops:"LLM Ops",
                graph:"Graph workflows — structure around the loop",
                compare:"Arena — race models and memory through the same loop",
                settings:"Behaviour — how a turn runs",
                database:"Database — everything Waku stores (state.db)",
                finance:"Finance — daily P&L and interview log"};
```

- [ ] **Step 3: Add the `VIEWS.finance` renderer**

In `waku/ops/static/js/views.js`, add a new entry to the `VIEWS` object
(alongside `models`, `connections`, `gateway`, etc. — order inside the object
doesn't matter, but keep it near `database` if that view exists in this file,
otherwise anywhere in the object literal):

```javascript
  finance(d){
    const entries = d.finance || [];
    const interviews = d.interviews || [];

    // Per-currency totals — CNY and USD are never summed together.
    const totals = {};
    for (const e of entries) totals[e.currency] = (totals[e.currency]||0) + e.pnl_amount;
    const totalTiles = Object.entries(totals).map(([cur, amt]) =>
      `<div class="tile"><b class="${amt>=0?"":"neg"}">${amt.toFixed(2)} ${esc(cur)}</b><span>total P&amp;L</span></div>`
    ).join("");

    const pnlRows = entries.length
      ? entries.map(e => `<tr>
          <td>${esc(e.date)}</td><td>${esc(e.account)}</td>
          <td class="${e.pnl_amount>=0?"":"neg"}">${e.pnl_amount>=0?"+":""}${e.pnl_amount} ${esc(e.currency)}</td>
          <td>${esc(e.note||"")}</td></tr>`).join("")
      : `<tr><td colspan="4" class="meta">No entries yet — tell Waku how an account did today.</td></tr>`;

    const interviewRows = interviews.length
      ? interviews.map(i => `<tr>
          <td>${esc(i.company)}</td><td>${esc(i.role)}</td><td>${esc(i.round||"")}</td>
          <td>${esc(i.status)}</td><td>${esc(i.notes||"")}</td></tr>`).join("")
      : `<tr><td colspan="5" class="meta">No interviews logged yet.</td></tr>`;

    return `<div class="tiles">${totalTiles || '<div class="meta">No P&amp;L logged yet.</div>'}</div>
      <h3 style="margin-top:20px">Daily P&amp;L</h3>
      <table class="datatable"><thead><tr><th>Date</th><th>Account</th><th>P&amp;L</th><th>Note</th></tr></thead>
      <tbody>${pnlRows}</tbody></table>
      <h3 style="margin-top:20px">Interviews</h3>
      <table class="datatable"><thead><tr><th>Company</th><th>Role</th><th>Round</th><th>Status</th><th>Notes</th></tr></thead>
      <tbody>${interviewRows}</tbody></table>`;
  },
```

This reuses existing CSS classes (`tile`, `meta`) already defined in
`style.css` for the Overview tab's tiles; if `.datatable`/`.neg` aren't
already defined there, add minimal rules to `waku/ops/static/style.css`:

```css
.datatable{width:100%;border-collapse:collapse;margin-top:8px}
.datatable th,.datatable td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--border)}
.neg{color:var(--danger, #c0392b)}
```

(Check `style.css`'s `:root` tokens first — reuse an existing negative/error
color variable instead of hardcoding `#c0392b` if one already exists, to
stay consistent in both light and dark themes.)

- [ ] **Step 4: Manually verify in the browser**

Run: `make dashboard`
Then: open `http://localhost:7777`, hard-reload, click **Finance** in the
sidebar. Confirm:
- the tab loads with no console errors
- typing "log today's A股 pnl: -300" in the chat dock, then reloading the
  Finance tab, shows the new row and an updated CNY total
- typing an interview report, then reloading, shows it under Interviews

- [ ] **Step 5: Commit**

```bash
git add waku/ops/static/index.html waku/ops/static/js/main.js waku/ops/static/js/views.js waku/ops/static/style.css
git commit -m "feat(dashboard): add Finance tab (daily P&L + interview log)"
```

---

### Task 6: Gate and ship

**Files:** none (verification only)

- [ ] **Step 1: Run the full deterministic gate**

Run: `make gate`
Expected: all deterministic evals pass (judge tier only runs if a provider
key is set — not required for this feature).

- [ ] **Step 2: Push via PR (main is protected)**

```bash
git push -u origin finance-interview-log
gh pr create --fill
gh pr checks --watch
```

- [ ] **Step 3: Merge once green**

```bash
gh pr merge --squash --delete-branch
```
