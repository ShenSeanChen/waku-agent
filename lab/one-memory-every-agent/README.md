# One memory, every agent

Grok Bot and Meta's Muse are personal agents that live on their own cloud
computers and remember you. Both keep that memory inside their own walls: you
cannot export it, and neither can hand it to the other. This topic connects
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
| Muse ([muse.ai](https://muse.ai)) | No MCP. Ask Muse to build a custom connector to Waku Memory's web API with a key: search is `GET /memories?q=…`, and saving is `POST /imports`, which Waku Memory turns into memories in the background | none exposed |

Grok Bot's connector form takes a URL and headers, and its MCP sign-in never
runs (a public issue, 2026-09-07), so it needs a Waku Memory key from waku.one
→ Account → Keys. Muse has no MCP at all, which makes its row the real
experiment: can an agent build its own connector to a memory it has never
seen? Claude Code, Codex and the Waku agent sign in through the browser.

Verified against: waku-agent 0.1.8 connecting to Waku Memory's MCP server, 2026-09-14. Grok Bot and Muse have not been run yet.

## The two harnesses, side by side

| Job | Grok Bot | Muse |
|---|---|---|
| Inbox and daily digest | a "chief of staff" routine across Slack, email and calendar | a Gmail connector with read/send permissions you set; Sentinel approves each send |
| Web research and errands | a browser on the shared computer; one sign-in is shared by every Bot | a browser sub-agent that sees a simplified page and cannot run JavaScript; PYMNTS gave it 3 errands and it completed 0 |
| Recurring work | routines on a schedule or an event, up to 50 per Bot | turns long-term goals into plans; no routines documented |
| Buying things | not a focus: its official workflows stop before anything is sent | Stripe Link single-use cards, and it asks every time |
| Who it is for | 8 official use cases, all work: sales, recruiting, ads, incidents | life: recipes from saved reels, dinner parties, bills |
| Teaching it | record 10 minutes of your screen and it becomes a skill; shareable templates | no skills exposed; it builds its own connectors to public web APIs |
| Security | one VM for all your Bots: "isolate personalities, not compute" | a VM per user, plus Sentinel, a separate agent that approves every action; the agent only sees stand-in credentials |
| Memory | kept per Bot, with no view, no edit and no export; deleting a Bot wipes its memory but keeps its files | a MEMORY.md file in the VM that you can read, and a "forget" skill, but no export; Meta says it "may still remember" what you deleted |
| Outside tools | custom MCP servers: public URLs only, with a key header | no MCP; custom connectors to public web APIs |
| Where and what it costs | desktop and mobile; bundled with SuperGrok and Cursor plans | US only; free, with paid tiers |

Sources, read 2026-09-15: [xAI, Introducing Grok Bot](https://x.ai/news/introducing-grok-bot) (2026-08-11) ·
[Grok Bot docs](https://docs.x.ai/grok-bot/bots) · [Grok Bot use cases](https://docs.x.ai/grok-bot/use-cases) ·
[Vellum teardown](https://www.vellum.ai/blog/official-grok-bot-breakdown) (2026-08-20) ·
[Grok MCP sign-in issue](https://github.com/ergofobe/imogen-server/issues/27) (2026-09-07) ·
[Meta, Introducing Muse](https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/) (2026-09-08) ·
[Meta, Muse security](https://research.meta.ai/blog/security-and-safety-for-ai-agents-our-approach-with-muse) ·
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
  Muse keeps what it knows about you in a text file you can read but cannot
  take with you.
- **The finding to test on camera:** neither agent exports its memory, so give
  both of them one memory they can reach, and see which one can actually use it.
- **The objection to raise yourself:** why hand your memory to a third company?
  Because you can see it, edit it and export it, which neither vendor offers.
  Others sell shared memory too (Mem0's OpenMemory, MIND's Grok plugin).
- **Boards**, each drawn from code so a fact change is a one-line edit:
  - [harness-jobs.excalidraw](whiteboards/harness-jobs.excalidraw) (`build_harness_jobs.py`):
    what a personal agent harness is for. Six jobs, and the five parts every
    job needs. The last part, memory, is the one you cannot take with you.
  - [grok-vs-muse.excalidraw](whiteboards/grok-vs-muse.excalidraw) (`build_grok_vs_muse.py`):
    the two system designs side by side. Solid boxes are what the vendor
    documents; dashed boxes are our inference, because neither company
    publishes how memory is stored or how the agent plans.
  - [one-memory-every-agent.excalidraw](whiteboards/one-memory-every-agent.excalidraw) (`build_board.py`):
    the fix, with every agent reaching one memory.

## Graduation

Already in the product: `waku connect waku-memory` (`waku/tools/waku_memory.py`)
and `waku skill export` (`waku/memory/procedural/exporter.py`). Saving skills
into Waku Memory waits until Waku Memory has a place for skills. If Muse can
use the web API, Waku Memory may want a documented recipe for agents that have
no MCP.
