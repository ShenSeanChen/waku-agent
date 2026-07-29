# Agent Graphs — system design

Status: phases 1–3 SHIPPED (engine + triage graph workflow behind
`WAKU_GRAPH_WORKFLOWS` + dashboard Graph tab). The content workflow (§4.2) and the
AutoManus port (§5) remain future work. Two shipped deviations from this doc:
triage has no `search_memory` fan-out node (the full path's retrieval gate already
covers it — a parallel prefetch would double-retrieve), and a `gather` fan-in node
sits before the router so it waits on both parallel branches.
Scope: a fifth pillar candidate — **Graph** — sitting beside Harness, Loop, Memory, Eval.

## 1. What we're adding and why

`waku/loop/agent.py` is one agent turn: a while-loop where the model picks tools until
it stops. That covers every conversational task. What it cannot express:

- **Parallel work** — three research calls that could run at once run one after another.
- **Explicit routing** — "if X go here, else go there" lives buried in prompt text, not
  in inspectable structure.
- **Multi-step pipelines** — generate → check (several checks at once) → publish-or-revise
  has shape; a single loop turn flattens it into one long transcript.

A **graph** makes that shape first-class: nodes (a function, an LLM call, or a whole
`run_loop` turn) connected by edges, some conditional, some parallel. The loop stays the
default path for chat; graphs are opt-in per workflow.

**Relationship to sub-agents/swarms** (for the docs): a graph is *structure*, not a kind
of agent. A node MAY be a sub-agent (a scoped `run_loop` call), but most nodes are plain
functions or single LLM calls. No peer-to-peer agent messaging, no swarm — execution
follows the edges, deterministically. That determinism is the point: you can trace it,
eval it, and explain it on a whiteboard.

## 2. Decision: no LangGraph

Build a small engine in-repo. Reasons, in Waku terms:

1. **No new dependencies** rule — core stays stdlib + anthropic/openai.
2. The teaching bar: "each pillar legible on its own." A ~200-line engine you can read
   beats a framework you have to trust. Same trick as `loop/agent.py`: show the whole
   mechanism in one file.
3. Our needs are: fan-out/fan-in, conditional edges, bounded cycles, tracing. That is a
   topological sort plus a thread pool — not a framework's worth of surface.

Revisit only if graphs become the center of gravity (checkpointing/resume across
processes, distributed nodes). Not now.

## 3. The engine — `waku/graph/`

```
waku/graph/
  engine.py       # Graph, Node, run_graph — the whole mechanism, one file
  nodes.py        # node factories: llm_node, tool_node, agent_node, router helpers
  workflows/      # one file per real workflow (triage.py, content.py, ...)
```

### 3.1 Core model (engine.py)

```python
NodeFn = Callable[[GraphState], dict]      # reads state, returns keys to merge
RouteFn = Callable[[GraphState], str]      # reads state, returns next node's name

@dataclass
class Node:
    name: str
    fn: NodeFn
    max_visits: int = 1        # >1 only on nodes that sit inside an intended cycle

class Graph:
    def add_node(self, node: Node) -> None
    def add_edge(self, src: str, dst: str) -> None            # unconditional
    def add_router(self, src: str, route: RouteFn,
                   targets: dict[str, str]) -> None           # conditional
    # special names: START, END

def run_graph(graph: Graph, state: dict,
              observer: Observer | None = None,
              max_steps: int = 25) -> GraphState
```

- **State is a single dict** (the blackboard). Every node receives the whole state and
  returns a dict of keys to merge. No typed channels, no reducers — a newcomer can
  print(state) at any point and understand the run. Parallel branches write **disjoint
  keys** (enforced: merging a key another live branch already wrote raises — collisions
  are a graph bug, not a race to silently lose).
- **Execution**: ready-queue over the DAG. A node is ready when all its in-edges have
  fired. Ready nodes run **concurrently on a ThreadPoolExecutor** (stdlib; the whole
  codebase is sync — asyncio would force a rewrite of tools and models for zero gain).
- **Routers** are ordinary Python functions over state — not LLM calls by default. When
  a decision needs a model, the node *before* the router makes the LLM call and writes
  e.g. `state["score"]`; the router just reads it. Keeps routing testable with 0/1 evals.
- **Cycles**: allowed only via an explicit router edge pointing backwards, guarded twice:
  per-node `max_visits` and global `max_steps` — the same two-guardrail pattern as
  `run_loop`'s natural-stop + max_iterations.
- **Errors**: a node exception is written to `state["errors"][node]` and routes to END
  through an optional `on_error` target — mirrors `ToolRegistry.execute`'s
  surface-don't-crash rule.

### 3.2 Node types (nodes.py) — factories, not classes

Everything is a `NodeFn`; these helpers just build common ones:

| factory | wraps | typical use |
|---|---|---|
| `tool_node(fn, in_keys, out_key)` | plain function | DB lookup, FTS5 search, ICS read |
| `llm_node(prompt_template, out_key, small=True)` | ONE model call, no tools | classify, score, extract |
| `agent_node(system, tools, out_key)` | a full `run_loop` turn with a **scoped ToolRegistry** | a step that genuinely needs multi-step tool use |
| `human_node(question, out_key)` | pause; gateway asks, answer resumes | approval gates (phase 2+) |

`agent_node` is the sub-agent story: same loop, same tracing, just a narrow tool subset
and its own working memory. Graphs don't replace the loop — they arrange calls to it.

### 3.3 Observability

`run_graph` takes the **same `Observer` signature** as `run_loop` and emits:
`graph_start`, `node_start`, `node_end` (with duration + state keys written), `route`
(router name + chosen target), `graph_end`. Inside an `agent_node`, the inner loop's
`llm`/`tool` events pass through with a `node=` field. Traces land in the same JSONL →
the dashboard gets a graph timeline view later without touching the engine.

### 3.4 Evals

- `evals/deterministic/test_graph_engine.py` — pure-function nodes, no LLM:
  topology order, fan-out runs in parallel (assert wall-clock < sum), fan-in waits for
  all branches, router picks correct edge, disjoint-key enforcement raises, cycle stops
  at max_visits, error path routes to on_error, max_steps hard stop.
- Each shipped workflow gets its own deterministic eval with LLM nodes stubbed
  (inject fake `NodeFn`s), plus judge evals on real end-to-end output where scoring
  is fuzzy (content quality).

Gate rule unchanged: `make gate` before any push; live bug → fix + regression case.

## 4. Workflows in Waku (use cases 2 & 3)

### 4.1 `workflows/triage.py` — inbound message triage (phase 2)

For messages arriving via telegram/discord gateways: decide fast, in parallel, what an
incoming message needs before the main loop ever spends a big-model turn on it.

```
START
  ├─ classify_intent   (llm_node, small model: question/task/urgent/social)
  ├─ search_memory     (tool_node: FTS5 semantic store)
  └─ check_calendar    (tool_node: today's events from calendar.ics)
        ↓ (fan-in)
  route(intent, urgency):
      social/trivial → quick_reply   (llm_node, small model)         → END
      task/question  → full_agent    (agent_node: the normal loop,
                                      pre-loaded with memory+calendar context) → END
      urgent         → notify_now    (tool_node: send_message)       → END
```

Value over today: the three context fetches run at once, and trivial messages never
touch the big model. This is the retrieval-gate idea generalized from one gate to a
structure.

### 4.2 `workflows/content.py` — draft → parallel checks → publish/revise (phase 3)

```
START → draft (agent_node: writing tools)
  ├─ check_tone       (llm_node, small)
  ├─ check_facts      (agent_node: search tool only)
  └─ check_length     (tool_node: pure function)
        ↓ (fan-in) → aggregate (tool_node: min of scores + collected feedback)
  route(score):
      pass            → save_note → END
      fixable         → revise (llm_node, gets feedback) → back to checks   [max_visits=2]
      unsalvageable   → human_node → END
```

Demonstrates the bounded cycle and the human gate — the two riskiest engine features —
in a low-stakes personal task.

## 5. AutoManus — lead qualification (use case 1)

Different repo, different constraints. Design here, build separately.

- **Where**: the FastAPI backend (`app-backend-AutoManus`), NOT the Next.js frontend —
  "AI agents live in the backend" rule. The backend repo is not cloned in this session,
  so this section is a spec.
- **How**: port the ~200-line engine pattern into `ai_utils/graph/` (copy, don't share a
  package — the repos stay independent; the engine is small enough that a fork is
  cheaper than a shared dependency).
- **Endpoint**: `POST /api/v1/leads/qualify` (verify_token + business scoping as usual).

```
START (contact_id, business_id)
  ├─ fetch_history     (Supabase: whatsapp_messages filtered by agent_id, deals)
  ├─ research_company  (LLM + web/company enrichment)
  └─ recent_activity   (last-touch recency, channel, response latency)
        ↓ (fan-in) → score (LLM node → {score, reasons})
  route(score):
      hot  (≥0.8)      → draft_followup (existing follow-up plan machinery) → END
      warm (0.5–0.8)   → add_to_nurture → END
      cold (<0.5)      → tag_and_skip   → END
```

- **Credits**: deduct via the credit service BEFORE the LLM nodes, per repo convention.
- **Frontend**: one hook (`useLeadQualification`) calling the endpoint; results render
  on the deal — no graph engine in the frontend at all.
- **Output contract**: `{score, band, reasons[], action_taken, drafted_message?}` so the
  UI and the daily "who to chase" brief consume the same shape.

## 6. Phasing

| phase | deliverable | proves |
|---|---|---|
| 1 | `graph/engine.py` + `nodes.py` + full deterministic eval suite | the mechanism, with zero LLM spend |
| 2 | `workflows/triage.py` wired behind a flag + tracing events | real value on the live assistant |
| 3 | `workflows/content.py` | cycles + human gate |
| 4 | AutoManus backend port + `/leads/qualify` | the pattern travels |

Each phase is one PR (topic branch → gate → squash-merge), shippable alone.

## 7. Decisions taken (flag disagreement before phase 1)

1. **No LangGraph** — in-repo engine, stdlib only.
2. **Threads, not asyncio** — matches the sync codebase; parallelism is I/O-bound
   (API calls), so the GIL is irrelevant.
3. **Dict blackboard with disjoint-key writes** — over typed state/reducers; legibility
   wins, collisions fail loudly.
4. **Routers are code, not LLM calls** — models produce state, functions read it.
5. **Graphs are opt-in structure around the loop** — `run_loop` untouched; `agent_node`
   composes it. The loop pillar's file does not change.
6. **Copy the engine into AutoManus** rather than extracting a shared library.
