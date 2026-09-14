# Commands

The `waku` command is installed with the package. In a checkout, the `make`
targets are aliases for the same things, plus the eval and tracing tools.

## waku

| Command | Does |
|---|---|
| `waku` | chat in the terminal |
| `waku dashboard` | the live cockpit at localhost:7777 (+ Telegram if `TELEGRAM_BOT_TOKEN` is set) |
| `waku voice` | talk to it — the "waku waku" wake word, or push-to-talk (needs the `[voice]` extra) |
| `waku telegram` | message it from your phone (needs `TELEGRAM_BOT_TOKEN`) |
| `waku discord` | answer in a Discord server (needs `DISCORD_BOT_TOKEN`) |
| `waku whatsapp` | answer WhatsApp messages (needs `WHATSAPP_TOKEN` and a public URL) |
| `waku brief` | a morning briefing from calendar + mail + memory, run as a loop |
| `waku gather` | the same job as a graph: four sources fetched together, then one digest |
| `waku connections` | every integration and its health, including Waku Memory |
| `waku connect google` | sign in to Google Calendar (opens your browser) |
| `waku connect waku-memory` | connect Waku Memory, the memory shared with your other agents (opens your browser) |
| `waku mcp` | MCP servers, and which account each one knows you as |
| `waku mcp login <name>` | sign in again — as someone else, or after expiry |
| `waku mcp logout <name>` | forget a server's token |
| `waku skill install <url>` | install a community skill from a GitHub or Gist link |
| `waku skill export --to claude,codex` | copy Waku's skills to Claude Code and Codex (`--project` for `./.claude/skills`, `--force` to replace) |

In the dashboard chat, `/connect google` and `/connect waku-memory` do the same
as their `waku connect` commands, and `/help` lists the graph workflows.

## make

| Command | Does |
|---|---|
| `make run` | chat with Waku in the terminal |
| `make dashboard` | the dashboard at localhost:7777 (restart it after pulling backend changes) |
| `make voice` | push-to-talk, or always-on with `WAKU_WAKE_WORD` |
| `make telegram` · `make discord` · `make whatsapp` | the messaging gateways |
| `make brief` · `make gather` | the morning briefing, as a loop or as a graph |
| `make eval` | deterministic evals (0/1, no judge involved) |
| `make eval-judge` | LLM-as-judge evals (scored %, needs an API key) |
| `make gate` | the release gate: deterministic must pass, judge must clear its threshold |
| `make lint` | ruff over the code and the evals |
| `make trace` | trace waterfalls in Phoenix at localhost:6006 |
| `make shootout RUNS="…"` | the same tasks on different models, e.g. `RUNS="kimi:kimi-k3 anthropic:claude-opus-4-8"` |
| `make shootout-coding RUNS="…"` | a coding round through pi, scored by tests |

Tests live in `evals/`, not `tests/`. [evals.md](evals.md) explains the two
kinds.
