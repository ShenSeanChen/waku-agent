# Treg company brain

A company brain is the boring, valuable thing an agent should have: what your
competitors shipped, who mentioned you, what reviewers complained about. This
topic pulls those facts in through treg's priced tool catalog, decides which
ones are worth keeping, and puts them somewhere every agent you own can read.

## The question

When an agent gathers facts about the outside world, what earns a place in
memory, and whose memory does it land in?

## What we connect

| Piece | What it does here |
|---|---|
| treg ([treg.to](https://treg.to)) | one token over ~60 providers: competitor ads, brand mentions, reviews, enrichment. Reaches Waku as a remote MCP server in `WAKU_HOME/mcp.json`, so no new code and no new dependency |
| Waku's loop and tools | `tools/mcp_client.py` registers treg's endpoints as `treg_*`; the loop calls them like any other tool |
| Waku's local memory | `memory/` — facts, episodes and skills in `.waku/state.db`, plus the regenerated `.waku/MEMORY.md` mirror |
| Waku Memory ([waku.one](https://www.waku.one)) | the hosted store Claude Code, Codex, Cursor, Hermes and Grok Bot already share |
| Jev ([typesafe.ai](https://typesafe.ai)) | the typed judge that scores whether a gathered fact earns a slot. `lab/jev-system-one/` already has the memory suite and the client |

Verified against: waku-agent 0.1.8 and treg.to as read, 2026-10-01. Nothing run yet.

## Run it

Not yet. The first run is the experiment below, and it needs a treg token:

```
export TREG_TOKEN=...                  # treg.to -> account
waku connect waku-memory               # the hosted store, browser sign-in
# add treg to WAKU_HOME/mcp.json as a streamable-HTTP server with auth_env
make run
```

## What we found

Three facts, all checked in the code, before a single call was made:

- **Waku Agent is the one agent that does not feed Waku Memory.** Claude Code,
  Cursor and Hermes all appear in the hosted store as import scopes
  (`import:claude-code-memory:`, `import:skill:cursor/`,
  `import:hermes-memory:`). Waku Agent appears only as project notes a human
  wrote. `tools/waku_memory.py` says why, on purpose: *"The two stores are
  separate: nothing here copies local memory up."* `skills/waku-memory/SKILL.md`
  and the SOUL rules go further and forbid claiming otherwise.
- **The hosted store does not want everything anyway.** Its import model takes
  authored content whole and free: `SKILL.md` as procedure, and
  `MEMORY.md`/`CLAUDE.md`/`AGENTS.md` as semantic knowledge. Episodic and fact
  rows are capture-only and never imported. So "sync it all up" is the wrong
  shape; Waku already regenerates `.waku/MEMORY.md` after every turn, which is
  exactly the shape the importer accepts.
- **Nothing ranks what memory gets into the prompt.** `memory/__init__.py`
  searches `retrieval_top_k` facts plus three episodes and joins all of them
  into the system prompt. There is a gate deciding *whether* to retrieve
  (`memory/retrieval_gate.py`) and nothing deciding *which of these is worth a
  slot* — which is the question `lab/jev-system-one`'s memory suite already
  asks, and the one a brain full of scraped competitor data makes urgent.

## Video angle

Hook: your agent can look up anything and remember nothing worth keeping.

The surprising finding is the one above, and it is a little embarrassing on
purpose: the product whose pitch is "one memory, every agent" ships the one
agent that does not put its memory there. Saying that out loud, then fixing it
on camera, is worth more than a clean demo.

The arc: gather with treg, judge with Jev, keep in Waku Memory, open the same
brain in a second harness. Boards to film live in `screenshots/`.

## Graduation

Nothing yet. Three candidates, in the order they would earn it:

1. **A treg row in the integrations registry** — bottom rung, config only.
2. **A slot gate in `memory/`** — the ranked half of retrieval, with its own
   deterministic eval and a cost-weighted judge eval beside the one that
   already covers the retrieval gate.
3. **An export path from local memory to Waku Memory** — `MEMORY.md` and
   `skills/` only, never the episodic rows, matching what the importer takes.
   This is a product decision as much as a code one, and it needs Sean and Ian.
