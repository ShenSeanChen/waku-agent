# Status

**What is true right now.** Rewritten whole when it changes, never appended to
— a status file that grows is a changelog, and git already is one.

Read this before opening a PR or filing an issue: most of what is already
known-broken is below, and half of it already has a fix in flight.

**Last updated:** 2026-08-28

---

## What works

The four pillars run: the loop, memory (semantic + episodic + procedural with
a retrieval gate), tools, and both eval tiers. `waku`, `waku dashboard`,
`waku voice`, `waku telegram`, `waku discord` and `waku brief` all start.

**635 deterministic evals pass offline**, no API key needed. CI runs them on
every PR along with ruff, the skills validator, and a check that
`.env.example` still matches the integrations registry.

**Remote MCP servers landed 2026-08-27** (#151, #152). A server already
running elsewhere is named by `url` instead of `command`, authorised either by
an API key in an environment variable or by signing in through the browser —
`{"oauth": true}`, no key to issue or hand over. `waku mcp` prints which
account each server knows you as. See `integrations.md`.

## Known broken

Nothing here is a surprise. If you hit one of these, the issue exists.

| What | Where | Fix in flight |
|---|---|---|
| The model picker offers OpenAI models that 404 on use | #137 | — |
| GPT-5.6 tool calls fail on Chat Completions | — | #146 |
| Dashboard crashes on legacy Windows consoles (cp1252) | #140 | #142 |
| Two delegate eval tests fail on Windows (shebang fixture) | #141 | #143 |
| OpenCode Zen fails with a rate-limit error | #112 | #113 |
| Judge evals split across two conventions | #135 | — |

**Providers are the recurring theme.** Five of the items above are one
provider or another, and there is no single place that says which providers
are known-good today. Until there is, treat the model picker as a list of
things that *might* work.

## What is deliberately not built

Not a framework, not multi-agent, not production — see `architecture.md`.

Additionally, and worth stating because people ask:

- **No Windows CI.** Two Windows bugs (#140, #141) were both found by
  contributors, not by us. Every Windows claim in this repo is untested.
- **The release gate is not in CI.** `make gate` runs deterministic evals at
  100% plus a judge threshold, and CI runs only the first half. The judge tier
  needs an API key, which CI does not have.
- **No provider smoke check.** Nothing verifies that a model in the picker
  resolves, which is why #137 reached a user.

## Open questions

1. **Where the memory pillar ends and Waku Memory begins.** This repo's memory
   is local, single-machine, and yours. Waku Memory is the same memory across
   several agents, and it is a paid hosted service. Both are true and the
   README does not yet say either plainly, so a reader has to work out the
   difference alone.
2. **Whether `docs/` should hold anything that is not reference.** Filming
   notes and one-time reports were removed on 2026-08-28 for this reason; the
   rule that keeps them out is not yet written down anywhere.

## Not in the repo

Deliberately absent, so nobody goes looking:

- Filming and demo notes — production material, not product documentation
- Session handoffs — this file replaces them
- Plans and specs — they belong with the work, not in `docs/`
