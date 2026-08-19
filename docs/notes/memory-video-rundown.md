# Memory-layer video — filming rundown

Four acts. For each beat: what is on screen, what you run, and the one thing to
watch for. Everything here was measured on 2026-08-13/15 against mem0ai 2.0.17,
zep-cloud 3.27.0, langmem 0.0.30, langgraph 1.2.10.

**Before you roll**, three things (see "Pre-flight" at the bottom) or Act 3 will
show contaminated numbers.

---

## ACT 1 — The three questions (~6 min)

**On screen:** `docs/whiteboards/memory-anatomy.excalidraw`, full frame.

**The spine.** Every memory system answers three questions, and most arguments
about AI memory are two people answering different ones:

| | question | options |
|---|---|---|
| Q1 | what SHAPE is it stored in? | file · rows · graph |
| Q2 | how do I FIND it again? | don't · keyword · vector (RAG) · graph traversal |
| Q3 | what happens to what is NO LONGER TRUE? | nothing · overwrite · invalidate · delete |

**The unlock, and the correction most people need:** RAG is a **Q2** answer, not
a storage type. "Graph vs RAG" is a category error — Zep does vector search
*over* graph nodes, so it is both.

**Then open Q3, because it was never a question about the store.** It is the
MANAGER, and it has four jobs:

- **DECIDE** — add / update / delete / noop
- **RETIRE** — what happens to what stopped being true
- **ATTRIBUTE** — which turn produced this
- **REFLECT** — when does the system think about its own memory

**Close the act on the empty column.** Every system in this video consolidates
**forward-only, on the critical path**. It only ever sees the recent window, so
it structurally cannot merge duplicates written months apart. **Nobody sleeps
on it.**

> Two terms to be careful with on camera. **"Context graph"** is not a
> standardised term — define it yourself in one line or skip it. **"Graph
> embedding"** is a real, specific technique (node2vec, GNNs) that embeds graph
> *structure*; I have **no evidence Zep does this** — what I verified is that it
> embeds *text* on nodes and edges. Do not say "Zep uses graph embeddings"
> without checking their docs first. It is the most correctable line available.

---

## ACT 2 — Each product on its own terms (~12 min)

**On screen:** VS Code with one file open at a time, and a terminal. Nothing
else. Each script is ~60 lines and imports **nothing** from waku.

Each file opens with a banner naming which box on the chart it demonstrates —
so cutting from whiteboard to terminal never loses the viewer.

### 2a — LangMem · THE MANAGER, Job 1

**Show:** `examples/memory-native/langmem_native.py`

```bash
.venv/bin/python examples/memory-native/langmem_native.py
```

**The beat:** three sentences in, **two memories out**. It read the whole
conversation and resolved the contradiction *before writing anything*.

```
kept : User's product launch is scheduled for June (updated from May - date was moved).
```

**Say:** the store here is a dict. **The manager IS the product.** And there is
no dashboard to open, because there is no server — it is a library sitting in a
list next to two hosted products, which is what confuses people about it.

### 2b — mem0 · THE MANAGER, Jobs 1 + 2

**Show:** `examples/memory-native/mem0_native.py`

```bash
.venv/bin/python examples/memory-native/mem0_native.py
```

**Beat one — it rewrites you.** You say a sentence, it stores a different one,
and it inferred a year nobody mentioned.

**Beat two — the trap, and the best 20 seconds in the video.** mem0 marks the
dead fact dead (`lifecycle_state: superseded`, `replaced_by`). **And search
returns it anyway:**

```
exact   : When is the product launch?
       -> 0.3429  [active    ]  ...scheduled for June 2026
          0.3474  [superseded]  ...scheduled for May 2026     <- dead row scored HIGHER
chinese : 发布会是什么时候?
       -> 0.1788  [superseded]  ...scheduled for May 2026
          0.1787  [active    ]  ...scheduled for June 2026     <- 0.0001 apart
```

**Say:** the "it's bad at Chinese" reading is wrong and I nearly published it.
The two rows sit on top of each other; **which one wins is noise.** Ask twice in
the same language and you can get both.

**The correction to make out loud:** mem0 **has** graph memory — there is a
Graph page and an Entities page in its console. It is not a per-call flag in
`mem0ai 2.0.17`, so we never enabled it. **What this measures is mem0's
default**, which is what you get when you start. Calling mem0 "a row store" as a
product-level fact is wrong, and the first viewer who has used graph mode will
say so.

### 2c — Zep · THE STORE (graph) + Job 2

**Show:** `examples/memory-native/zep_native.py`, then **app.getzep.com → your
project → Users → the quickstart user → Graph**.

```bash
.venv/bin/python examples/memory-native/zep_native.py
```

**The beat:**

```
The product launch is scheduled for May.  ->  INVALID from 2026-08-12T21:24:47Z
The launch moved to June.                 ->  still valid
```

Not out-ranked. **Marked invalid at a point in time**, and still queryable as
history.

**Then show the graph view** — entities as nodes, relationships as edges. Point
at `Product Launch Date` (large, typed by the ontology) beside `product launch`
(small, extracted generically). **The ontology is a schema; the graph is what
the extractor built to fit it.**

### 2d — the ending Act 2 has earned

**All three mark the dead fact dead. All three hand it back from a plain
search.** Read the lifecycle field, or ship the wrong date.

---

## ACT 3 — The arena (~8 min)

**On screen:** `make dashboard` → localhost:7777 → **Arena → Memory**.

> **Restart the dashboard first.** Python does not hot-reload. This has caught
> us three times.

### The setup block, top to bottom

1. **Questions** and **Model** — one row, one decision
2. The contestants — `sqlite · mem0 · langmem · zep · control`
3. **Tell N stores** / **Ask N stores**

**Say why there are two buttons:** telling is 53% of a race and never changes,
so it is its own button. **Tell once, ask as many times as you like.** Ask tells
anything not yet told, so it is never the wrong button to press.

### The control is the star

`control · no memory` — **told nothing, asked everything.** Any probe it passes
was never testing memory in the first place.

**Say the number:** three of seven of my original probes were answerable with
zero memory. I was asking "what does Jensen always wear" and scoring the
model's training data. **I have not seen another memory comparison publish a
control.**

### Then race, and read the table

Watch the badge say **`told 4 of 8`** while it seeds.

### Then "What each store is holding" → Read stores

All five cards describe the **same seeding**, so they are genuinely comparable.
`sqlite` reads the race's own `.waku-arena/` copy — **not** your live agent.

**The beat to look for — and to film rather than fix:**

```
sqlite:  design-review — Moved to Wednesday.
         design-review — Moved to Wednesday.     <- twice
zep:     Marcus will be out of office.
         Marcus will be out of office.           <- twice
```

**That is Act 1's "DECIDE: adds only" gap, demonstrated on your own system.**
The consolidator never checks whether it already knows something. Filming your
own missing column is the part people believe.

---

## ACT 4 — The four jobs, and the ending (~5 min)

**On screen:** `docs/whiteboards/memory-in-harness.excalidraw`.

**The one-box argument, drawn to scale.** Swap `WAKU_SEMANTIC_STORE` and only
the semantic store changes. The loop, the retrieval gate, `SKILL.md`, the
episodic log, the consolidator, the harness, the gateways — untouched. **One box
of seven. That is the entire memory-layer market.**

**Then waku's own four jobs, as gaps:**

- DECIDE — adds only, never updates, never deletes
- RETIRE — nothing automatic
- ATTRIBUTE — has a `source` field, no link back to the turn
- REFLECT — per-turn, on the critical path

**And the ending. Do NOT pitch waku memory.**

You spent this build discovering, on camera-able evidence, that your memories
are in four different vendors' accounts; that deleting a user does not clean a
project; that a schema you agreed to in a setup wizard shapes what your agent
can learn. **That is a lock-in story you hit personally, live.**

> "My memories are in four places and none of them are mine. That's what I'm
> working on next. It's not ready. The repo's open."

**Position waku-agent as what it is: the test rig.** It is the thing that let
you find all of this. That is a far better pitch than "please star my repo" —
it just demonstrated its value for 25 minutes.

---

## Pre-flight — do these before you roll

**1. Clean the hosted accounts** (removes the leftover quickstart partitions):

```bash
.venv/bin/python scripts/arena_clean.py
```

Read the list, confirm nothing there is precious, then:

```bash
.venv/bin/python scripts/arena_clean.py --yes
```

**2. Housekeeping:**

```bash
rm -rf $TMPDIR/memarena-* .jarvis.bak-20260711-131255 .jarvis.prerename-bak
```

**3. Zep needs a fresh project.** Its project-wide ontology — set by the
onboarding wizard, visible under *Project Wide Customization* — is inherited by
**every** user in the project, and deleting users never clears it. Two
brand-new users, told only our three sentences, both came back holding `CTR`,
`brand deal`, `rough cut` and `retention rate`.

**Until this is done, Zep's arena column is not publishable.** Everything else
is.

---

## Numbers you can state on camera

| claim | value | where it came from |
|---|---|---|
| mem0: add() → queryable | **~14s**, no readiness signal at all | measured, `_settle` in `mem0_native.py` |
| Zep: `processed=True` | true while the graph held **zero** nodes | measured on a clean project |
| mem0: superseded row's score | **0.3474** vs 0.3429 for the live one | live run |
| LangMem | 3 sentences → **2** memories | live run |
| Zep | `INVALID from <timestamp>` | live run |
| leaked temp dirs before the fix | **656** | `ls $TMPDIR/memarena-*` |

**One number to re-measure live rather than quote:** the token cost of a full
loop turn versus `examples/tiny_memory_agent.py`. Run both on camera and read
the receipts off the screen — it is more convincing than a figure I read out,
and the tool schemas dominating the prompt is the whole point.

---

## What NOT to claim

- **Do not say Zep uses graph embeddings** without checking their docs
- **Do not say mem0's graph mode is paywalled** — unverified. Say "we ran the
  default, which is what the free tier gives you"
- **Do not present the arena's Zep column** until the fresh project exists
- **Do not present the duplicate facts as a bug you fixed** — they are a gap you
  are showing, and the honesty is the point
