# Status

**What is true right now.** Rewritten whole when it changes, never appended to
— a status file that grows is a changelog, and git already is one.

Read this before opening a PR or filing an issue: most of what is already
known-broken is below, and half of it already has a fix in flight.

**Last updated:** 2026-11-23

---

## What works

The four pillars run: the loop, memory (semantic + episodic + procedural with
a retrieval gate), tools, and both eval tiers. `waku`, `waku dashboard`,
`waku voice`, `waku telegram`, `waku discord`, `waku brief` and
`waku connect google` all start. Plugins install from a marketplace, and
their `mcp_tool` hooks run at SessionStart, UserPromptSubmit, PreToolUse and
Stop (spec 021). `command` hooks, which run a program on this machine, are
not built.

`waku serve` starts (spec 023). It stores each Telegram
message, Slack @mention and GitHub issue or mention as a row in `state.db`,
and one worker answers them one conversation at a time. A message that arrives
during a restart is answered after it. Each reply is a row in the `outbox`
table, and a dispatcher sends it to the Telegram chat, the Slack thread or the
GitHub issue it answers, retrying a failed delivery four times. `send_message`
writes a row in the same table. `schedule_task` sets a timer that fires once, or repeats every day or every weekday at a time on the person's clock (spec 033): the
agent names a time and a note, and `waku serve` starts a run with that note in
the same conversation within about 5 seconds of that time. "Send me a brief every morning at 9" sets one repeating timer, and each morning's run reads the connected services, memory and the team board and sends one short message where it was asked. A tool call that writes
to someone else waits for a yes, and so does a reply to a GitHub issue or a
Slack channel and a write through an MCP server (spec 025: a server on the
starter list asks for its writes, any other server for every tool it does
not declare read-only, and Waku Memory never asks): `send_message` to a GitHub issue or to another chat is stored, the owner in `WAKU_OWNER` is asked on Telegram, and
the stored call runs when the owner answers "yes". `waku waiting`, the tools
`list_waiting` and `cancel_waiting`, and the "Waiting" card on the dashboard's
Overview list the timers, the waiting calls and the queued messages, and
cancel one. The Slack and GitHub
handlers, all three senders and the approval path have run against fake
events, fake answers and a fake `gh` only, never against a live chat,
workspace or repository.

A person connects an MCP server of their own in one step (spec 025): `waku mcp add`, a card on the Connections page, or "connect Linear" in chat under `waku serve`, which asks the owner first. The starter list holds Linear, Notion and GitHub, and `waku connections` checks the Slack tokens and names a missing scope. The sign-in to a vendor's server, the three starter servers and a Slack app made from the manifest have run against a stub sign-in and a fake Slack only, never against the live services.

**4,739 deterministic evals pass offline**, with no API key; 60 more are live
evals that skip without one. CI runs the offline tier on every PR along with
ruff, the skills validator, and a check that `.env.example` still matches the
integrations registry.

**0.1.8 is on PyPI and on GitHub Releases.** Pushing a `v*` tag publishes to
both, so the repo's "Latest" release always matches `pip install waku-agent`.

**The dashboard uses the Waku Memory design system**, and
`test_design_system.py` keeps it from drifting. See
[context/design-system.md](context/design-system.md).

## Known broken

Nothing here is a surprise. If you hit one of these, the issue exists.

| What | Where | Fix in flight |
|---|---|---|
| The model picker offers OpenAI models that 404 on use | #137 | #178 |
| GPT-5.6 tool calls fail on Chat Completions | — | #146 |
| OpenCode Zen fails with a rate-limit error | #112 | #113 |
| `send_message` has no address book, so "message Alex" is refused: it takes `owner` or an address. In `waku` and `waku dashboard` it reaches only the owner and refuses anyone else, because only `waku serve` can ask for a yes | — | — |
| A message `send_message` queues waits until `waku serve` runs; `waku` and `waku dashboard` do not send it | — | — |
| `waku serve` can deliver a chat reply twice when it dies after the channel accepted the reply and before the row is marked `sent` | — | — |
| `waku serve` and the dashboard's Telegram cannot share one bot token: Telegram allows one poller, and the second one stays idle. `waku serve` says "telegram is blocked" on the line that lists its sources; the dashboard does not | — | — |
| The sign-in link that `connect_tool` and the Connections page give works only on the computer Waku runs on, because the server sends the browser back to a port on that computer. On a hosted dashboard these cards read "not available here". There a person signs in to Notion or Linear at `agent.waku.one/setup` (spec 029, group A), and to GitHub, read-only, on a server that holds a GitHub App's client id and secret (group F), the gateway keeps the tokens, and the agent calls the connector's read tools through the metering proxy. A tool that writes is refused there, because nobody on hosted Waku can be asked first yet (spec 029, group C). This has run against a fake vendor only, never against Notion, Linear or GitHub. On a server that holds a Google client's id and secret, a person also connects one Gmail mailbox there, and the agent searches it, reads a thread and writes a draft reply with `mail_search`, `mail_read` and `mail_draft` (spec 031, part A). Nothing sends a mail, new mail does not wake the agent, and it has run against a fake Google only | — | — |
| Slack, Gmail and Google Drive are not on the starter list: each vendor's server takes sign-in only from a client registered with it beforehand, and Waku registers itself at sign-in | — | — |
| The owner can approve only in a private Telegram chat named in `WAKU_OWNER`. Without one, a call that needs a yes is refused. Slack cannot carry the answer, because `waku serve` hears no Slack direct message | — | — |
| The model reads a call that waits for a yes as "A plugin hook stopped this call before it ran", followed by the real reason | — | — |
| A timer `schedule_task` sets fires only while `waku serve` runs, or in a hosted container; `waku` and `waku dashboard` on your own machine do not fire it. A repeating timer and the daily brief have run against a fake clock and no model only: no live morning, no live Slack, and no brief a model wrote has been read. On hosted waku a stopped container is started for a timer or a Slack mention, and nothing else wakes it yet: no chat or embed message is stored as an event (spec 026, group B). A hosted container sends to Slack only, so a run a timer starts outside a Slack thread sends nothing. This path has run against a fake spawner, a fake clock and a fake Slack only, never on the VM. The same is true of the restart that moves a running hosted container to a new image between two turns after an upgrade: it has never compared image ids on a real Docker daemon | — | — |
| The public Waku Slack app ("Add to Slack" on hosted waku) is built and has never run against Slack: the Slack app itself is not created, and `config/gateway.env` on the VM has none of its four values. One workspace maps to one tenant, and the hosted app has no channel allowlist. That tenant is a person, or the home of a team space's agent (spec 032, group A: `POST` and `GET /v1/team-homes` for waku.one, a team's Waku Memory key sealed in `control.db`, and a container that saves into the team's space). A team home has run against a fake Slack and a fake spawner only: `WAKU_AGENT_PLATFORM_SECRET` is set nowhere, `DELETE` answers 405, and a workspace a person's install holds is not moved. A direct message to the bot is answered for the person who added the app and for nobody else (spec 032, version A): it has run against a fake Slack only, and the live app has neither `im:history`, `message.im` nor its Messages tab yet. The Slack card on a hosted dashboard has been drawn from a stub answer only, and it has no Disconnect | — | — |
| Google Calendar sign-in has no bundled OAuth client, so `waku connect google` needs your own `~/.waku/credentials.json` | — | — |

**Providers are the recurring theme.** `scripts/provider_smoke.py` sends one
tool turn to each provider you have a key for and prints which answered. A
model server on your own machine works through the `local` row (spec 034): no
key, Ollama's address by default, and a model that supports tool calls.

## What is deliberately not built

Not a framework, not multi-agent, not production — true of `waku/`, see
[architecture.md](architecture.md). `hosted/` runs that same code as a
deployment instead; spec 001 designs it, and none of it exists in this repo
yet.

Additionally, and worth stating because people ask:

- **No Windows CI.** The Windows bugs so far (#140, #141, both fixed) were
  found by contributors, not by us. Every Windows claim in this repo is
  untested.
- **The judge evals are not in CI.** `make gate` runs deterministic evals at
  100% plus a judge threshold, and CI runs only the first half. The judge tier
  needs an API key, which CI does not have.
- **The provider smoke check is not in CI.** It needs real keys, so a model
  that stops answering is found only when someone runs it.

## Open questions

1. **Where the memory pillar ends and Waku Memory begins.** This repo's memory
   is local, single-machine, and yours. Waku Memory is the same memory across
   several agents, and it is a paid hosted service. Both are true and the
   README does not yet say either plainly, so a reader has to work out the
   difference alone. Hosted waku (`hosted/`, spec 001) adds a third case: a
   tenant's memory lives on the operator's VM, not the person's own machine,
   but it is still that one tenant's own directory, not Waku Memory. What
   hosted waku keeps there is not written down yet; a later task in spec 001
   records it once `hosted/` exists.

## Not in the repo

Deliberately absent, so nobody goes looking:

- Filming and demo notes — production material, not product documentation
- Session handoffs — this file replaces them
- Plans and specs — they belong with the work, not in `docs/`
