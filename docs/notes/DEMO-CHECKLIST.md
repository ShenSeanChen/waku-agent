# Demo / filming checklist

The workflow to walk through on camera, in order. Each beat has the exact prompt, what it
proves, where to look, and whether it's been dry-run verified. Keep this updated as we test.

## Pre-flight

- [x] Provider = `anthropic` (best streaming; Gemini breaks multi-turn tool use) — Models
- [x] Free `TAVILY_API_KEY` pasted on the Connections page (for the World Cup beat)
- [x] Clean curated state — `python scripts/demo_seed.py --yes` (clears Loop/Tools traces + Ops eval history; keeps `usage.jsonl` spend unless you add `--reset-spend`). Refuses without `--yes` — it's destructive.
- [x] `waku dashboard` running on your own machine (also starts Telegram if a token is set) → `localhost:7777` in a real browser

## The beats

| # | Beat (pillar) | Say this | Watch | Verified |
|---|---|---|---|---|
| 1 | Cockpit tour (Harness) | — (click around) | Overview: stats, gate bar, clickable diagram | [x] |
| 2 | Gateways (Harness) | chat from `make run` **and** the browser | Gateway tab tags each `cli` / `dashboard` | [x] |
| 3 | The Loop + streaming | *"Schedule a tennis game with Raj this Saturday at 8am"* | reply streams; LOOP box pulses; Loop tab `iter 2` | [x] |
| 4 | Calendar read | *"What's on my calendar today?"* | `list_events` fires; answers from `state.db` | [x] |
| 5 | Retrieval gate (Memory) | *"When am I swimming with Sergey?"* then *"what's 12 × 8?"* | gate retrieve vs skip; Overview bar; Ops decisions | [x] |
| 6 | Memory self-management | *"Remember Raj prefers morning tennis"* | `save_note`; Memory ▸ Semantic + `MEMORY.md` update | [x] |
| 7 | **Multi-tool loop (money shot)** | *"Search the World Cup games still left and add each to my calendar"* | Loop tab `iter 8`: `search_web` × N → `create_event` × N | [x] |
| 8 | Consolidation (Memory) | keep chatting past N exchanges | Memory ▸ Consolidation; a new episode + distilled facts | [x] |
| 9 | Telegram gateway | message the bot from your phone | Gateway tab shows it tagged `telegram` | [x] |
| 10 | **Voice** | `waku voice` — hands-free, say "waku waku, …" (or click the dock mic) | WAV → local Whisper; landed in a distinct `voice` conversation | [x] |
| 11 | Eval / LLM-Ops (hero 2) | `make gate` in a terminal | prints `GATE OPEN`; Ops ▸ Eval history gains a row | [x] |
| 12 | Spend ledger | (just look) | Ops: all-time cost/tokens, per-day — survives resets | [x] |
| 13 | **Database tab** | click each table tab; run a query in the **SQL console** | per-table schema (indigo headers) + rows; `SELECT` returns live data (and persists) | [x] |
| 14 | Ops walkthrough | (just look) | Ops: eval-history table, per-turn gate decisions, slowest turns, inline JSONL traces | [x] |
| 15 | Markdown chat + Gateway inbox | (bonus) | replies render bold/tables/lists; Gateway = channel-tagged conversation inbox, telegram live | [x] |

## Status: all 15 beats dry-run verified ✅

Everything above works on the live dashboard. Three real gateways proven (web / telegram /
voice), all four pillars, both hero moments. Remaining is optional polish, not testing —
e.g. a top-to-bottom README pass as the on-camera script, and `demo_seed.py` for a clean take.

## Where the git history lives

Every feature above is a commit on `main` with a WHY-focused message — `git log --oneline` is the
changelog. Key ones: streaming, `search_web` (World Cup loop), `list_events` (calendar read),
permanent spend ledger + `MEMORY.md`, source-tagged Gateway, coming-soon skeletons.

## Reset between takes

`python scripts/demo_seed.py --yes` — clears the memory/calendar **and** the Loop/Tools traces + Ops eval
history for a clean take, and backs up the whole `.waku` first. The `usage.jsonl` spend ledger is
**kept** (it's a permanent record) unless you pass `--reset-spend`. It never deletes the db file, so a
running `waku dashboard`/`waku telegram` keeps working. Nothing else clears your data.
