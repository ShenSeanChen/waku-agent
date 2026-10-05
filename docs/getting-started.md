# Getting started

Five steps take you from nothing to a Waku that remembers you. Each step ends
with a check, so you know it worked before you move on. Everything runs on your
own machine.

## 1. Install

```bash
pip install waku-agent
```

To read or change the code, clone it instead:

```bash
git clone https://github.com/ShenSeanChen/waku-agent && cd waku-agent
uv venv && uv pip install -e .
```

### Other ways to run it

In a checkout, `uv run waku …` needs no venv activation. Three ways to run it:

| Command | When |
|---|---|
| `uv run waku dashboard` | quick start, zero activation (recommended) |
| `source .venv/bin/activate` → `waku dashboard` | activate once, bare `waku` all session |
| `uv tool install .` → `waku dashboard` | install `waku` **globally**, forever |

**Check:** `waku connections` prints a list of integrations.

## 2. Add one key

```bash
cp .env.example .env
```

Set `WAKU_PROVIDER=` and paste that provider's key. Anthropic is the default;
OpenAI, Gemini, DeepSeek, MiniMax, Kimi, GLM, OpenRouter, OpenCode Zen,
OpenCode Go and Opper work the same way. You can also paste a key in the
dashboard later. Either way the key stays in a `.env` on your machine and is
never sent to the browser.

Waku reads a model key from three places, and the first one that has it wins:
environment variables set before Waku starts, the `.env` in the folder you run
Waku from (or the nearest folder above it), and `~/.waku/.env`. A key you paste
in the dashboard goes to the folder's `.env` when there is one, and to
`~/.waku/.env` when there is not, so Waku finds it from any folder. When Waku
finds no key, the dashboard's "Set up Waku" page lists the paths it checked,
and `waku connections` prints the same list under "Model key". A git worktree
does not see the main checkout's `.env`: start Waku in the main checkout, or
copy the key's line into `~/.waku/.env`.

**Check:** run `waku` and say hi. It answers in the terminal.

## 3. Open the dashboard

```bash
waku dashboard          # → http://localhost:7777
```

`waku` and `waku dashboard` are two doors into the **same** Waku. The dashboard
is a small web server on your machine (`127.0.0.1`): the browser is the UI, and
the same process runs every turn. Set `TELEGRAM_BOT_TOKEN` and it starts your
Telegram bot too.

**Check:** send a message from the chat dock and watch the Overview diagram
light up as it flows through the harness.

## 4. Watch it remember

Say *"Remember that Alex prefers morning meetings."* Quit, and restart. Then
say *"Book a catch-up with Alex on Friday."*

**Check:** it books 9am, and **Memory ▸ Semantic** lists the fact. Your memory
is one file: `~/.waku/state.db`, the same from every folder. Set `WAKU_HOME`
to keep it somewhere else. If you ran Waku before v0.2, your memory is in the
`.waku/` folder you ran it from, and Waku keeps using it there until you copy
it: `mkdir -p ~/.waku && cp -R ./.waku/. ~/.waku/`.

## 5. Share memory with your other agents (optional)

[Waku Memory](https://www.waku.one) is a hosted memory that several agents
share, so a fact saved in Claude Code can be recalled here, and the other way
round.

```bash
pip install 'waku-agent[mcp]'           # in a checkout: uv pip install -e '.[mcp]'
waku connect waku-memory                # or /connect waku-memory in the dashboard chat
waku skill export --to claude,codex     # optional: carry Waku's skills too
```

Your browser opens once to sign in. Restart Waku to load the `waku_memory_*`
tools.

**Check:** `waku mcp` names the account you signed in as, and
`waku connections` lists Waku Memory as connected.

To connect Claude Code, Codex, Hermes or Grok Bot to the same memory, see
[integrations](integrations.md#share-one-memory-with-your-other-agents-waku-memory).

Beside it, [treg](https://treg.to) gives Waku live data when it researches,
on your own treg account:

```bash
waku connect treg                       # or /connect treg in the dashboard chat
```

**Check:** `waku connections` lists treg as connected, and so does the
**Connections** page. More in
[integrations](integrations.md#live-data-for-research-treg).

## Next

- [The tour](tour.md): the dashboard's tabs, things to try, and the loop up close.
- [Integrations](integrations.md): voice, Telegram, calendars and MCP servers.
- [Commands](commands.md): every `waku` and `make` command.
