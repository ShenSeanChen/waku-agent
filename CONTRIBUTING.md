# Contributing to Waku

Waku started as a teaching repo you could read in an afternoon, and it's growing toward a
full local-first assistant — the next Hermes / OpenClaw, with 1/100th the code. Contributions
are genuinely welcome. The project will get bigger; the one thing it must never do is get
*muddier*.

**The bar for every PR:** clear, self-contained, and tested. A newcomer should be able to open
the file you touched and follow what it does. New capability is great — complexity that hides
how the system works is what we push back on.

## The easiest contribution: a skill (no Python needed)

1. Copy [`skills/TEMPLATE.md`](skills/TEMPLATE.md) to `skills/community/<your-skill>/SKILL.md`
2. Fill in `name` + `description` (the Agent Skills frontmatter) and the body
3. Test locally: `python scripts/validate_skills.py`, then chat — your skill loads when it matches
4. Open a PR. CI runs the same validator.

Anyone can then try your skill instantly:
`waku skill install <link to your SKILL.md>`

## Code contributions

Good places to add real value:

- **Providers** (`waku/loop/models.py`): most models expose an OpenAI- or Anthropic-compatible
  endpoint, so a new provider is usually one `PROVIDERS` row — no new wire code. Add a pricing
  row in the dashboard and a case to `evals/deterministic/test_providers.py`.
- **Gateways** (`waku/gateway/`): receive/send for a new channel (WhatsApp, Discord, Slack,
  email). Keep it to one file; the CLI gateway is the reference.
- **Memory stores** (`waku/memory/semantic/`): match the `add`/`search` interface of
  `SqliteFactStore`. The Supabase adapter is the reference.
- **Tools** (`waku/tools/`): a new capability the agent can call. Follow `calendar.py` and the
  `new-tool` skill — schema, safe execution, honest output, and a deterministic eval.

Two rules that keep contributions safe to merge:

- **Test what you add.** Every behavior change gets a deterministic eval in
  `evals/deterministic/` (0/1, no network). If you found a bug, add the case that catches it.
- **Heavy or optional deps go behind an extra** (`[voice]`, `[telegram]`, `[voice-neural]`, …),
  never in the default install. No new core dependency without discussion.

Run the gate before pushing: `make gate` (deterministic must pass; judge evals run if you have
a key). `make lint` too. CI runs the gate on every PR — it must be green to merge.

## Where does my change go? — the footprint ladder

The core is a narrow waist; capability belongs at the edges. Every tool waku
registers is sent to the model on **every single call**, so the bar for adding
one is deliberately high. Start at the top of this ladder and only move down
when the rung above genuinely can't do it:

1. **Extend something that already exists.** A new provider is usually one
   `PROVIDERS` row. A new memory backend matches an existing interface.
2. **A skill** — `skills/community/<name>/SKILL.md`. Markdown, no Python, no new
   context cost until the model actually needs it. This is the easiest and most
   underrated contribution; see above.
3. **A CLI + a README.** waku can already run any program on your machine. A
   command-line tool with docs beside it costs nothing until it's used.
4. **A tool behind an extra** — `waku/tools/`, heavy deps gated by
   `[voice]`/`[notion]`/`[gcal]`-style extras, off by default.
5. **A gateway** — `waku/gateway/`, one file. Gateways only move text: in via
   `waku.respond()`, out again. No memory, no tools, no loop logic.
6. **A new core tool — last resort.** It has to earn its place in every prompt.

One thing the ladder deliberately has no rung for: **a new top-level package**
(like `waku/graph/`). That's not a contribution size, it's an architecture
decision — it needs a written design doc and a maintainer yes before any code
(see `docs/agent-graphs-design.md` for the precedent and the bar it had to clear).

If you're unsure which rung you're on, open an issue and ask before writing
code. That conversation is cheaper than a rejected PR.

## Scope — what we'll say no to, kindly

We welcome growth; we decline **complexity that muddies the core**: frameworks that hide the
loop, changes that bloat the default path for everyone, or features that can't be read and
tested on their own. When we say no, we'll explain why — and forking is always fair game
(that's what MIT is for).

Concretely, these get declined **even when the code is good**:

- **Speculative infrastructure** — an abstraction with no second caller yet. Add
  the second use case first; the right shape is obvious then and guessed now.
- **A new core dependency.** The default install is stdlib plus the two API
  clients. Heavy or optional things go behind an extra.
- **Anything that costs every user context** for a feature some users want —
  that's what the ladder above is for.
- **A behavior change with no deterministic eval.** If it can break, pin it.
- **Hidden network calls, reading `.env` or secrets, or running code at install
  time.** waku runs on people's own machines with their own keys.
- **A "fix" that removes the thing it secures** — e.g. sandboxing a tool by
  making it not work.

None of this is about the quality of your code. It's about what everyone who
installs waku has to carry.

## What you can expect from us

- **A first response within 48 hours** — even if it's "this needs a proper look,
  give me a few days." Silence is the one thing we try never to do.
- **Comment on an issue before you start and it gets assigned to you**, so two
  people never build the same thing. (This has already gone wrong once, and it
  cost someone a weekend.)
- **CI runs on your PR** — if it's your first contribution, GitHub needs a
  maintainer to approve the run. If it seems stuck, say so on the PR; that
  delay is ours, not yours.

## A note on safety

Because Waku runs on people's own machines with their own keys, PRs must never add hidden
network calls, read or transmit secrets/`.env`, or run code at install time. Keep it local,
keep it legible.

## Community

Questions, show-and-tell, pair-debugging: [Discord](https://discord.gg/7Ntxzm3eJ). By
contributing you agree your work is licensed under the repo's MIT license.
