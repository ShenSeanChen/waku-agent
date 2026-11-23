# One memory, every agent

Grok Bot and Meta's Muse are personal agents that live on their own cloud
computers and remember you. Muse lets you download its memory files, and
Grok Bot's docs don't say how its memory is stored; neither one shares memory
live with the other agents you use. This topic connects
both of them, together with Claude Code, Codex and the Waku agent, to one
memory, Waku Memory, and checks which of them can find a fact that another one
saved.

## The question

Can one memory follow you across Grok Bot, Muse, Claude Code, Codex and the
Waku agent?

## What we connect

| Agent | How it reaches Waku Memory | Skills |
|---|---|---|
| Waku agent | `waku connect waku-memory` | its own `skills/` |
| Claude Code | `npx waku-memory setup` ([waku.one/docs](https://www.waku.one/docs)) | `waku skill export --to claude` |
| Codex | `npx waku-memory capture enable` ([waku.one/docs](https://www.waku.one/docs)) | `waku skill export --to codex` |
| Grok Bot | Settings → Plugins → custom MCP connector: URL `https://api.waku.one/mcp`, header `Authorization: Bearer <key>` | its own: record a task on screen and it becomes a skill |
| Muse ([muse.ai](https://muse.ai)) | Meta doesn't mention MCP; Muse builds its own custom connectors. Ask it to build one to Waku Memory's web API with a key: search is `GET /memories?q=…`, and saving is `POST /imports`, which Waku Memory turns into memories in the background | none exposed |

Grok's connector docs allow custom MCP servers at public URLs, and a public
issue (2026-09-07) reports the sign-in flow not running, so use a Waku Memory
key from waku.one → Account → Keys. Meta doesn't mention MCP for Muse, which
makes its row the real experiment: can an agent build its own connector to a memory it has never
seen? Claude Code, Codex and the Waku agent sign in through the browser.

Verified against: waku-agent 0.1.8 connecting to Waku Memory's MCP server, 2026-09-14. Grok Bot and Muse have not been run yet.

## The two harnesses, side by side

| Job | Grok Bot | Muse |
|---|---|---|
| Inbox and daily digest | a "chief of staff" routine across Slack, email and calendar | a Gmail connector with read/send permissions you set; Sentinel approves each send |
| Web research and errands | a browser on the shared computer; one sign-in is shared by every Bot | a browser sub-agent that sees a simplified page and cannot run JavaScript; PYMNTS gave it 3 errands and it completed 0 |
| Recurring work | routines on a schedule or an event, up to 50 per Bot | turns long-term goals into plans; no routines documented |
| Buying things | no buying example; purchases are listed as needing your approval | Stripe Link single-use cards, and it asks every time |
| Who it is for | 8 official use cases, all work: sales, recruiting, ads, incidents | life: recipes from saved reels, dinner parties, bills |
| Teaching it | record 10 minutes of your screen and it becomes a skill; shareable templates | no skills exposed; it builds its own connectors to public web APIs |
| Security | one VM for all your Bots: "isolate personalities and workspaces, not compute" | a VM per user; Sentinel, a separate agent outside the agent's container on the same machine, approves actions; the agent only sees surrogate tokens |
| Memory | keeps working preferences, facts and summaries; how it is stored is not documented; deleting a Bot keeps shared files and sign-ins | memory files (MEMORY.md) you can read, edit and download; a "forget" skill; Meta says it may still remember what you deleted |
| Outside tools | account-wide connectors; Grok's connector docs allow custom MCP at public URLs | MCP not mentioned; it builds custom connectors |
| Where and what it costs | desktop and mobile; xAI's pages disagree on which plans include it | rolling out in the US; free, with paid tiers |

Sources, read 2026-09-15: [xAI, Introducing Grok Bot](https://x.ai/news/introducing-grok-bot) (2026-08-11) ·
[Grok Bot docs](https://docs.x.ai/grok-bot/bots) · [Grok Bot use cases](https://docs.x.ai/grok-bot/use-cases) ·
[Vellum teardown](https://www.vellum.ai/blog/official-grok-bot-breakdown) (2026-08-20) ·
[Grok connectors](https://docs.x.ai/grok/connectors) · [Grok MCP sign-in issue, third-party](https://github.com/ergofobe/imogen-server/issues/27) (2026-09-07) ·
[Meta, Introducing Muse](https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/) (2026-09-08) ·
[Meta, Muse security](https://research.meta.ai/blog/security-and-safety-for-ai-agents-our-approach-with-muse) · [How We Designed Muse](https://introducing.muse.ai/) ·
[Meta Help, Muse memory](https://www.meta.com/help/artificial-intelligence/1047255454427887/) ·
[PYMNTS errands test](https://www.pymnts.com/news/artificial-intelligence/2026/meta-muse-cannot-order-pizza-without-help) (2026-09-09).

## Run it

```bash
uv pip install -e '.[mcp]' && uv run waku connect waku-memory    # once, in a checkout
uv run python lab/one-memory-every-agent/check_memory.py           # save one fact, search it back
```

Use `uv run python`, not a bare `python`: on macOS that is the system 3.9,
and Waku needs 3.11 or newer.

The script saves one fact with a unique code word, in the scope
`lab:one-memory-every-agent` so it never mixes with your real memories. It
searches the fact back, then prints the question to ask each of the other
agents. `--forget <id>` removes the fact afterwards.

`build_grok_plugin.py` builds a plugin folder in the format of xAI's plugin
marketplace (`skills/` plus a `.mcp.json` with a key placeholder). That
marketplace serves **Grok Build**, xAI's coding agent, not Grok Bot, so the
folder is kept for a possible Grok Build segment; Grok Bot uses the custom
connector above.

## What we found

This table is filled in from real runs only.

| Agent | Connected | Found the check fact | Notes |
|---|---|---|---|
| Waku agent | yes, 2026-09-14 | not run yet | the stored sign-in expired and did not refresh; the next connect opened the browser |
| Claude Code | not run yet | not run yet | |
| Codex | not run yet | not run yet | |
| Grok Bot | not run yet | not run yet | does the key header work where sign-in does not? |
| Muse | not run yet | not run yet | can it build the connector, and can it save through `POST /imports`? |

## Video angle

- **Title:** Grok Bot vs Meta Muse: Who Owns What Your Agent Knows About You?
- **Hook:** Grok Bot gives your Bots separate personalities but one computer.
  Muse keeps what it knows about you in files you can download, but a download
  is a snapshot that stops updating.
- **The finding to test on camera:** neither agent shares its memory live with
  your other agents, so give both of them one memory they can reach, and see
  which one can actually use it.
- **The objection to raise yourself:** why hand your memory to a third company?
  Because it is one memory every agent reads live. Be plain that Waku Memory
  has no one-click export yet.
  Others sell shared memory too (Mem0's OpenMemory, MIND's Grok plugin).
- **Boards.** These are screenshots of first drafts; the editable sources stay
  private, and the boards filmed for the video are redrawn by hand.
  - [harness-jobs.png](screenshots/harness-jobs.png): what a personal agent
    harness is for. Six jobs, and the five parts every job needs. The last
    part, memory, is the one you cannot take with you.
  - [grok-vs-muse.png](screenshots/grok-vs-muse.png): the two system designs
    side by side. Solid boxes are what the vendor documents; dashed boxes are
    our inference, because neither company publishes how memory is stored or
    how the agent plans.
  - [one-memory-every-agent.png](screenshots/one-memory-every-agent.png): the
    fix, with every agent reaching one memory.

## Graduation

Already in the product: `waku connect waku-memory` (`waku/tools/waku_memory.py`)
and `waku skill export` (`waku/memory/procedural/exporter.py`). Saving skills
into Waku Memory waits until Waku Memory has a place for skills. If Muse can
use the web API, Waku Memory may want a documented recipe for agents that have
no MCP.
