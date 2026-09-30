---
name: waku-memory
description: Connect Waku Memory, hosted cross-agent memory in Claude Code, Codex, Grok Bot, Hermes. Export Waku skills.
---

## What Waku Memory is

This app's memory is local. It lives in `.waku/` on this machine and never
leaves it. Waku Memory (waku.one) is a separate, hosted memory that several
agents share over MCP: a fact saved in Claude Code can be recalled in Codex,
Grok Bot or this app. The two stores are separate, so never tell the user that
local memory syncs to Waku Memory.

## Connecting this app

Connecting opens the user's browser, so you cannot do it for them. Tell them to
type `/connect waku-memory` in the dashboard chat, or to run
`waku connect waku-memory` in a terminal. It needs the MCP extra:
`pip install 'waku-agent[mcp]'`. After it connects, a restart of Waku loads the
`waku_memory_*` tools.

## Connecting other agents

- **Claude Code, Codex, Hermes:** point them to https://www.waku.one/docs.
- **Grok Bot:** Settings → Plugins → add a custom connector with the URL
  `https://api.waku.one/mcp` and the header `Authorization: Bearer <key>`,
  using a key from waku.one → Account → Keys. Its connector form takes a URL
  and a header, not a browser sign-in, which is why it needs the key.
- **Muse Code or any other MCP client:** add `https://api.waku.one/mcp` as a
  remote (streamable HTTP) server.

Tell the user to paste a key only into the other agent's own settings, never
into this chat.

## Carrying skills

`waku skill export --to claude,codex` copies Waku's skills into Claude Code's
and Codex's skills folders. `--project` writes to `./.claude/skills`, which
Muse Code also reads. Saving skills into Waku Memory is not available yet.

## What not to claim

Don't quote prices, plans or limits; point to https://www.waku.one/docs.
