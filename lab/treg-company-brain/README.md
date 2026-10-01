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
| Waku's local memory | `memory/` keeps facts and episodes in `.waku/state.db` and skills in `.waku/skills/`. `.waku/MEMORY.md` is a view of those two tables that Waku rewrites after every turn |
| Waku Memory ([waku.one](https://www.waku.one)) | the hosted store Claude Code, Codex, Cursor, Hermes and Grok Bot already share |
| Hosted Waku ([agent.waku.one](https://agent.waku.one)) | the same agent, one container per person. Its gateway's default token audience is `https://api.waku.one/mcp`, the Waku Memory MCP address |
| Jev ([typesafe.ai](https://typesafe.ai)) | the typed judge that scores whether a gathered fact earns a slot. `lab/jev-system-one/` already has the memory suite and the client |

Verified against: waku-agent 0.1.8, waku-memory-backend b628ee9 and treg.to as read, 2026-10-01

## Run it

Nothing runs yet. The first run is the gather step below, and it needs a treg
token in your shell:

```
export TREG_TOKEN=...                  # treg.to -> account
waku connect waku-memory               # the hosted store, browser sign-in
# add treg to WAKU_HOME/mcp.json as a streamable-HTTP server with auth_env
make run
```

## What we found

Every fact below was checked in the code on 2026-10-01. No treg call has been
made yet.

- **Waku Agent is the one agent that does not feed Waku Memory.** Claude Code,
  Cursor and Hermes all appear in the hosted store as import scopes
  (`import:claude-code-memory:`, `import:skill:cursor/`,
  `import:hermes-memory:`). Waku Agent appears only as notes a person wrote by
  hand. `tools/waku_memory.py` states the separation on purpose: *"The two
  stores are separate: nothing here copies local memory up."*
  `skills/waku-memory/SKILL.md` and the SOUL rules in `runtime/session.py`
  forbid the agent from claiming otherwise.
- **The hosted store already accepts Waku as a source.** The backend's
  `Harness` enum has `waku`, migration `0023` widened both CHECK constraints,
  and the worker builds its accepted prefixes from the enum, so
  `waku-memory:` and `skill:waku/` are valid today. The gap is entirely on
  the client side.
- **`.waku/MEMORY.md` is not an authored document.** `Memory.export_markdown()`
  writes every row of the `facts` and `episodes` tables into it after each
  turn. Uploading that file would upload every episode, and it would store a
  new whole copy every time a single fact changed, because the importer
  deduplicates on content hash.
- **Claude Code's import is the model to copy.** Claude Code's memory
  directory holds one agent-written file per fact. Each file becomes one
  Knowledge memory in the scope `import:claude-code-memory:<file>`. Waku facts
  map onto that shape one-to-one: one fact becomes one memory, skills become
  Skills, and episodes stay on the machine.
- **A changed file does not replace its old copy.** The importer's idempotency
  key is `(user, session, content_hash)`, and the source path is not part of
  it. An edited fact therefore lands as a second memory beside the first. This
  is already true for Claude Code imports.
- **Nothing ranks what memory gets into the prompt.** `memory/__init__.py`
  searches `retrieval_top_k` facts plus three episodes and joins all of them
  into the system prompt. `memory/retrieval_gate.py` decides *whether* to
  retrieve, and nothing decides *which* retrieved fact is worth a slot. A
  brain full of scraped competitor data makes that second decision urgent.

## Video angle

Working title: "You Can Learn To Build Your Own Company Brain", a collab with
AI Jason (treg).

Hook: your agent can look up anything and remember nothing worth keeping.

The surprising finding is a little embarrassing on purpose: the product whose
pitch is "one memory, every agent" ships the one agent that does not put its
memory there. Saying that out loud, then fixing it on camera, is worth more
than a clean demo.

The arc has four steps, and each one must run on camera without a cut that
hides a manual step:

1. Gather competitor ads, brand mentions and reviews with treg.
2. Let Jev decide which gathered facts earn a slot.
3. Keep the chosen facts in Waku Memory.
4. Open the same brain in a second harness: Claude Code locally, and the chat
   on waku.one for viewers who never install anything.

Boards to film live in `screenshots/`.

## Graduation

Nothing has graduated yet. Five candidates, in the order they would earn it:

1. **A treg row in the integrations registry.** Bottom rung, config only.
2. **Waku Agent contributes to Waku Memory.** Facts upload one per memory,
   skills upload whole, and episodes stay local. The scanner side is
   waku-memory spec 031 task 3; the agent side is waku-agent spec 003, which
   also reverses the "never syncs" wording in three places.
3. **One home at `~/.waku`.** waku-agent spec 002, issue #249. The scanner
   needs one predictable place to look.
4. **A slot gate in `memory/`.** This is the ranked half of retrieval, with its
   own deterministic eval and a cost-weighted judge eval beside the one that
   already covers the retrieval gate.
5. **A chat box on waku.one.** It would put the hosted agent in the console's
   corner with access to every memory, including the company brain. It needs
   its own spec after the frontend is read.
