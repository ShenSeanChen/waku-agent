# Roadmap

The whiteboard has boxes beyond the flagship scheduling task. This page says
which ones are live, which are still skeletons, and how each default upgrades
when you outgrow it.

## The boxes beyond the flagship task

These live in [`waku/tools/experimental.py`](../waku/tools/experimental.py),
off by default; `WAKU_EXPERIMENTAL=1` registers them.

**Sub-Agents is live.** `delegate_task` hands a coding job to
[pi](https://github.com/earendil-works/pi) — Mario Zechner's minimal
open-source coding agent — through its headless print mode (`pi -p "task"`).
Waku stays the orchestrator (memory, context, evals); pi is the specialist
contractor (read/bash/edit/write). Try it:

```bash
npm install -g --ignore-scripts @earendil-works/pi-coding-agent
WAKU_EXPERIMENTAL=1 uv run waku
# "have pi fix the failing test in ~/my-project"
```

The full pi transcript lands in `.waku/outbox/delegate-*.log`; tune the budget
with `WAKU_DELEGATE_TIMEOUT` (default 300s). The pi walkthrough is in
[lab/pi-agent/](../lab/pi-agent/README.md).

The rest are still deliberate **skeletons**: the intent is drawn so the diagram
maps to something, but nothing is over-promised. They report "coming soon",
and the dashboard's **Tools** tab lists them under **Coming soon**:

| Whiteboard box | Tool | Status |
|---|---|---|
| Sub-Agents | `delegate_task` | **live** — delegates coding tasks to pi |
| Graph workflows | [`waku/graph/`](../waku/graph) | **live** behind `WAKU_GRAPH_WORKFLOWS=1` — [triage-first turns](tour.md#graph-workflows) |
| Terminal tool | `run_command` | skeleton — needs a real sandbox + safety surface first |
| Browser tool | `browse_web` | skeleton — `search_web` already covers read-only lookups |
| Cron Job | `schedule_task` | skeleton — `make brief` + a system cron line covers it today |

A teaching repo's point is a readable core, so these come alive one at a time,
tested.

## Upgrade paths

| Default (zero setup) | Upgrade | How |
|---|---|---|
| SQLite FTS5 keyword memory | Supabase pgvector semantic search | `WAKU_SEMANTIC_STORE=supabase` + [sql/init_supabase.sql](../sql/init_supabase.sql) — the exact schema from [launch-rag](https://github.com/ShenSeanChen/launch-rag)/[launch-agentic-rag](https://github.com/ShenSeanChen/launch-agentic-rag) |
| Mock calendar (ICS + SQLite) | Apple / Google Calendar | `WAKU_APPLE_CALENDAR=1` (macOS) or `WAKU_GOOGLE_CALENDAR=1` with `pip install -e '.[gcal]'` — the tool schema stays |
| Hand-built memory pillars | mem0 / Zep / LangMem | `pip install -e '.[arena]'` and set `WAKU_SEMANTIC_STORE` — then race them against each other in the Arena's Memory tab. [Where to see your memories in each provider's own console](memory-backends-playbook.md) |
| Local memory on one machine | Waku Memory, shared across agents | `waku connect waku-memory` — see [integrations](integrations.md#share-one-memory-with-your-other-agents-waku-memory) |

## Related repos (the building blocks)

[launch-rag](https://github.com/ShenSeanChen/launch-rag) ·
[launch-agentic-rag](https://github.com/ShenSeanChen/launch-agentic-rag) ·
[launch-agent-skills](https://github.com/ShenSeanChen/launch-agent-skills) ·
[launch-mcp-demo](https://github.com/ShenSeanChen/launch-mcp-demo) ·
[launch-DeepResearch-Backend](https://github.com/ShenSeanChen/launch-DeepResearch-Backend)
