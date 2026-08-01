# milli/ops — the Eval/LLM-Ops pillar

Everything here answers one of two questions: **what did the agent just do?**
(tracing, the dashboard) and **is it any good?** (arena, judge, scoring, the
release gate). Nothing here is part of the agent loop — you can delete this
whole directory and milli still runs. That's deliberate: ops observes, it never
participates.

`static/` has its own map — see [static/README.md](static/README.md) for the
frontend.

## Backend map

| File | Owns |
|---|---|
| `dashboard.py` | The stdlib HTTP server: routes, SSE, `collect()`. Serves everything below. |
| `browser_agent.py` | The ONE shared `Milli` behind the browser gateway + its dated chat session. |
| `arena.py` | Racing N models through the same harness, in isolated temp homes. |
| `catalog.py` | What models a provider can serve + your pinned `provider:model` shortlist. |
| `pricing.py` | `$/M` rate tables, knowledge cutoffs, and the spend ledger summary. |
| `settings_api.py` | Reading the live config, and swapping provider/model without a restart. |
| `compare_history.py` | The arena's own JSONL scoreboard. Never `state.db`. |
| `judge.py` | LLM-as-judge: one reply in, a graded score out. |
| `scoring.py` | Deterministic completion scoring — did the right tool fire, with the right args? |
| `coding_eval.py` | The coding battery used when a race has `delegate_task` switched on. |
| `tracing.py` | The JSONL trace writer every gateway appends to (+ optional OTel). |
| `show_trace.py` | `milli trace` — reading those files back in the terminal. |
| `release_gate.py` | `make gate`: deterministic must pass, judge must clear the threshold. |
| `brief.py` | The morning brief. |
| `whiteboard/` | Excalidraw generators for the architecture diagrams in `docs/`. |

## Which way the arrows point

```
dashboard  ──→  arena  ──→  pricing        scoring · judge · compare_history
    │                        ↑
    ├───────→  settings_api ─┼─→  catalog  ──→  pricing
    │                        │
    └───────→  browser_agent ┘        (settings_api also rebuilds the agent)
```

One rule keeps this readable: **arrows never point back up.** `catalog` doesn't
know settings_api exists; `pricing` doesn't know anything exists. If you find
yourself needing an import that reverses one of these arrows, the function is
probably in the wrong file.

`dashboard.py` is the only module that knows what an HTTP request is. Everything
else takes plain Python arguments and returns plain dicts — which is why they're
testable without starting a server, and why `evals/deterministic/` can call them
directly.

## The one global

`browser_agent` holds a module-level agent shared by every browser tab, because
the dashboard is multi-threaded and long-lived in a way the CLI is not. Two
callers mutate it (`dashboard` builds it on the first chat, `settings_api`
rebuilds it on a provider switch), so **import the module, not the name**:

```python
from milli.ops import browser_agent
browser_agent.current()          # sees a later swap
```

```python
from milli.ops.browser_agent import _agent   # frozen at None forever
```
