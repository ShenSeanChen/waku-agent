# One memory, every agent

Waku Memory is one hosted memory that several agents share over MCP. This
topic connects it, together with Waku's skills, to five agents, and checks that
a fact saved in one of them can be recalled in each of the others.

## The question

Can one memory follow you across Grok Bot, Muse Code, Claude Code, Codex and
the Waku agent?

## What we connect

| Agent | Memory | Skills |
|---|---|---|
| Waku agent | `waku connect waku-memory` | its own `skills/` |
| Claude Code | `npx waku-memory setup` ([waku.one/docs](https://www.waku.one/docs)) | `waku skill export --to claude` |
| Codex | `npx waku-memory capture enable` ([waku.one/docs](https://www.waku.one/docs)) | `waku skill export --to codex` |
| Muse Code | a streamable HTTP server at `https://api.waku.one/mcp` in `~/.config/muse/settings.json` | `waku skill export --project` (Muse reads `.claude/skills`) |
| Grok Bot | Settings → Plugins → custom connector, URL `https://api.waku.one/mcp`, header `Authorization: Bearer <key>` | the Waku plugin folder from `build_grok_plugin.py` |

Grok Bot runs in xAI's cloud, and its connector form takes a URL and a header
rather than a browser sign-in, so it needs a Waku Memory key from waku.one →
Account → Keys. Every other agent signs in through the browser. The Muse Code
settings path and the Grok Bot form come from third-party guides, not from our
own runs yet.

Verified against: waku-agent 0.1.8 connecting to Waku Memory's MCP server, 2026-09-14. Grok Bot (Grok 4.6) and Muse Code (Muse Spark) have not been run yet.

## Run it

```bash
pip install 'waku-agent[mcp]' && waku connect waku-memory     # once
python lab/one-memory-every-agent/check_memory.py              # save one fact, search it back
```

The script saves one fact with a unique code word, in the scope
`lab:one-memory-every-agent` so it never mixes with your real memories. It
searches the fact back, then prints the question to ask each of the other
agents. `--forget <id>` removes the fact afterwards.

Grok Bot gets Waku's skills as a plugin folder:

```bash
python lab/one-memory-every-agent/build_grok_plugin.py        # writes grok-plugin/, ignored by git
```

The folder holds `skills/` and a `.mcp.json` that names Waku Memory's server
with `${WAKU_MEMORY_API_KEY}` in place of a key. It never holds a real key.

## What we found

This table is filled in from real runs only.

| Agent | Connected | Found the check fact | Notes |
|---|---|---|---|
| Waku agent | yes, 2026-09-14 | not run yet | |
| Claude Code | not run yet | not run yet | |
| Codex | not run yet | not run yet | |
| Muse Code | not run yet | not run yet | |
| Grok Bot | not run yet | not run yet | does it expand `${WAKU_MEMORY_API_KEY}` in `.mcp.json`? |

## Video angle

- **Hook:** Grok Bot lives in the cloud, Muse Code lives in your terminal, and
  they share one memory.
- **The finding to test on camera:** Grok Bot cannot sign in to an MCP server,
  so it is the one agent that needs a key.
- **Board:** [whiteboards/one-memory-every-agent.excalidraw](whiteboards/one-memory-every-agent.excalidraw),
  drawn by `build_board.py`.

## Graduation

Already in the product: `waku connect waku-memory` (`waku/tools/waku_memory.py`)
and `waku skill export` (`waku/memory/procedural/exporter.py`). Saving skills
into Waku Memory waits until Waku Memory has a place for skills. If the Grok
plugin works, it becomes a published plugin; which repo owns it is still open.
