# Loop vs graph engineering

> A loop **discovers** what to do next.
> A graph **pre-determines** what happens next.

Both ship in this repo, doing the same job, so you can read them against each
other: `waku brief` is a loop, `waku gather` is a graph. Everything below was
measured on those two.

---

## The ladder — each rung is something you took control of

The buzzwords arrived in an order, and the order isn't marketing. Each one is a
different part of the system you stopped leaving to chance.

| | you control | you stopped worrying about |
|---|---|---|
| **prompt engineering** | the words | phrasing |
| **context engineering** | what's in the window | what the model can see |
| **skills** | the procedure | *how* it does a known task |
| **loop engineering** | what happens between calls | tools, memory, retries |
| **graph engineering** | the shape of the calls | order and concurrency |

Two honest caveats, so nobody catches you out:

- **These overlapped; they didn't replace each other.** Context engineering and
  loops grew up together, and RAG sat in the middle of both.
- **Everyone still does all five.** A graph doesn't retire your prompts.

### The rung most people skip: skills

> **A skill is a procedure written in English that the model *may* follow.
> A graph is the same procedure written in code that it *cannot not* follow.**

This repo has both. `skills/weekly-brief/SKILL.md` describes the morning routine
step by step, and the loop reads it and mostly complies — but on a bad day it
skips a step, reorders, or forgets a source. The graph version can't skip a node.

That gives you a rule for "should I build a graph?"

**Write the skill first. Harden it into a graph only once it stops changing.**

A skill is ten seconds to edit, no code, no deploy. A graph you have to rewrite.
Pay that cost when the shape has settled, not before.

---

## When each one wins

**The loop wins when you can't draw the steps in advance.**

"Work this PR" — read the diff, run the tests, discover it needs a rebase,
decide what next. Step three depends on what step two found. Any graph you
commit to is wrong by then. This is most of what an assistant does.

**The graph wins when you already know the shape and parts of it can overlap.**

"Brief me every morning" — GitHub, the web, the calendar, memory. You don't
discover that list; you know it before you wake up. Letting a model work it out
one tool call at a time buys nothing and costs a round trip each time.

---

## The measurement

Same task, both engines, against this repo. Full output in the commit history.

**The loop** — the model decides what to call:

```
 5701ms  tool  github_read
 6078ms  tool  github_read
 6740ms  tool  list_events
 TOTAL: 15297ms
```

**The graph** — the shape is known, so the scans run together:

```
    0ms  START scan_github · scan_web · scan_calendar · scan_memory
 1889ms  done  scan_github   (1506ms)
 1890ms  done  scan_web      (1887ms)
 1890ms  done  scan_calendar    (2ms)
 1890ms  done  scan_memory      (1ms)
13170ms  done  synthesize   (11279ms)
 TOTAL: 13171ms
```

### Read this carefully, because the headline number is the boring one

**Gathering: 1.9s vs 6.7s — 3.5×.** That's the real effect.

**Totals: 13.2s vs 15.3s — 14%.** Because an 11-second synthesis call dominates
both runs.

> **A graph speeds up the part that can be parallel, and nothing else.**
> If your workflow is one big model call wearing a hat, a graph buys you almost
> nothing.

### The finding that isn't about speed at all

The loop made **three** tool calls. It never searched the web and never searched
memory. It didn't fail — it decided it had enough and stopped.

The graph fetched **four**, because the shape says four.

> **A graph can't forget.**

For something you run every morning, that reliability is worth more than the two
seconds. This is the argument to lead with.

---

## "Isn't this just deterministic workflows from 2023?"

Mostly yes, and you should say so. Airflow, Temporal and Step Functions have run
conditional parallel DAGs for a decade. The shape is old.

What's actually new is narrow, and it's worth being precise about:

1. **The nodes are non-deterministic.** A node is a model call; the same input
   can produce different output.
2. **A model can pick the edge.** Routing is data-dependent in a way a DAG
   scheduler never had to handle.
3. **So you need guardrails Airflow never needed.** waku's engine caps visits
   per node and total steps, because a graph with a model in it can otherwise
   loop forever with perfect confidence.

Old shape, new contents.

---

## The thing worth realising

Open waku's architecture diagram — gateway → memory → loop → tools → reply,
same order every turn.

**That's already a graph.** It was just hardcoded in Python instead of nodes and
edges.

So a graph engine doesn't give you the shape. You had it. What it gives you is
the shape as **data instead of code**: inspectable, drawable *from the code
itself*, partly parallel, and re-routable without rewriting a function.

That's the honest pitch. It's smaller than the hype and more useful.

---

## How it's built here

```
waku/graph/engine.py             nodes, edges, waves — the whole mechanism
waku/graph/nodes.py              tool_node / llm_node / agent_node / key_router
waku/graph/workflows/triage.py   the per-message front door
waku/graph/workflows/gather.py   the morning routine
waku/ops/gather.py               where the pure workflow meets this machine
```

**Three ideas carry the engine.** State is one dict every node reads and merges
into — parallel nodes must write disjoint keys, and a collision raises rather
than silently losing a write. Routers are plain Python functions over that
state: models write it, code reads it, and **no model ever decides control flow
directly**. Guards are the loop's two-guardrail pattern generalised — per-node
visit caps for bounded cycles, plus a global step cap.

**The loop becomes one node.** `agent_node` runs a whole `run_loop` turn. This
is the part people miss: a graph doesn't replace your loop, it *arranges calls
to it*. In `triage`, the `full_agent` node is the ordinary loop, unchanged.

**Waves, not a scheduler.** Nodes whose dependencies are all satisfied run
together in a thread pool; the next wave waits for the slowest. That's a real
trade — a fast branch idles at the barrier — taken for legibility. The dashboard
prints the wait rather than hiding it.

### The two workflows are two different jobs

|  | **triage** | **gather** |
|---|---|---|
| Trigger | every message, automatically | you — `make gather` |
| Frequency | every turn | once a morning |
| `WAKU_GRAPH_WORKFLOWS` | gated by it | **ignores it** |

Worth stating plainly, because two charts on one page look like two options you
choose between. They aren't.

### Try it

```bash
make gather            # the graph — four sources at once
make brief             # the loop — same job, model decides
make dashboard         # localhost:7777 → Graph → Run gather
```

The Graph tab shows the topology **and** a live card row. The chart shows the
shape; the cards show it happening, with each wave's bars sharing a left edge
because the nodes shared a start.

---

## Safety, because a morning routine that acts on your behalf is a different animal

`gather` **proposes and never acts**. Not by instruction — by construction:

- no `agent_node`, no `run_loop`, no `ToolRegistry` anywhere in it
- its one model call passes **no `tools` parameter**, so the model is never
  handed a schema it could use to send, merge or create anything
- the only write is a markdown file in `.waku/outbox/` for a human to read

A source-level test fails CI if any of that changes, because the day someone
adds a tool-using node "to make the digest smarter", nothing else would notice.

**A model that is never given tools cannot use them.** That's a guarantee about
capability, not about instructions — and it's the only kind worth relying on.
