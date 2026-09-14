# Contributing to Waku

Waku started as a teaching repo you could read in an afternoon, and it's growing toward a
full local-first assistant — the next Hermes / OpenClaw, with 1/100th the code. Contributions
are genuinely welcome. The project will get bigger; the one thing it must never do is get
*muddier*.

**The bar for every PR:** clear, self-contained, and tested. A newcomer should be able to open
the file you touched and follow what it does. New capability is great — complexity that hides
how the system works is what we push back on.

**The rules live in [AGENTS.md](AGENTS.md).** It is short, it routes you to the one file that
covers your kind of change, and it lists exactly what CI blocks. If you work with a coding
agent, it reads AGENTS.md on its own.

## The easiest contribution: a skill (no Python needed)

1. Copy [`skills/TEMPLATE.md`](skills/TEMPLATE.md) to `skills/community/<your-skill>/SKILL.md`
2. Fill in `name` + `description` (the Agent Skills frontmatter) and the body
3. Test locally: `python scripts/validate_skills.py`, then chat — your skill loads when it matches.
   CI also checks that it does **not** load on everyday messages, and does not take over
   another skill's messages (`evals/deterministic/test_skill_triggers.py`). Skills load on
   shared words, so describe yours with words specific to its domain (*DAU*, *interview*,
   *standup*), not question words like *why / what / should / for* — those appear in almost
   every message anyone sends.
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

Before you write code, find two things:

- **Your tier.** A bug fix is just a PR. A new tool, gateway or dashboard view needs a short
  plan on the issue first. Anything that changes the loop, memory, the graph engine or the
  tool contract needs a proposal. See [conventions §2](docs/context/conventions.md#2-how-much-process-a-change-needs).
- **Your rung.** Where new capability goes is [the footprint ladder](docs/context/conventions.md#3-where-new-capability-goes-the-footprint-ladder).
  If you're unsure which rung you're on, open an issue and ask before writing code. That
  conversation is cheaper than a rejected PR.

## Sending a PR

- Run `make gate` and `make lint` before you push.
- CI runs ruff, the skills validator, the `.env.example` check and every deterministic eval.
  The judge evals need an API key, so CI does not run them; `make gate` does, if you have a key.
- The PR template asks how you tested your change. The review will ask too.

## Scope — what we'll say no to, kindly

We welcome growth; we decline **complexity that muddies the core**: frameworks that hide the
loop, changes that bloat the default path for everyone, or features that can't be read and
tested on their own. When we say no, we'll explain why — and forking is always fair game
(that's what MIT is for).

Concretely, these get declined **even when the code is good**:

- **Anything that breaks a hard rule in [AGENTS.md](AGENTS.md)** — a new core dependency, a
  behavior change with no deterministic eval, hidden network calls or secrets.
- **Speculative infrastructure** — an abstraction with no second caller yet. Add
  the second use case first; the right shape is obvious then and guessed now.
- **Anything that costs every user context** for a feature some users want —
  that's what the footprint ladder is for.
- **A "fix" that removes the thing it secures** — e.g. sandboxing a tool by
  making it not work.
- **A rename.** The name is tied to the videos, the PyPI package and the
  assistant's own identity. Fork it and rename freely — MIT only asks that you
  keep the attribution line.
- **Material about another project in `docs/` or the product** — whiteboards, write-ups or
  demos made for a video belong in `lab/`; see [conventions §6](docs/context/conventions.md#6-examples-and-video-material).

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

## Community

Questions, show-and-tell, pair-debugging: [Discord](https://discord.gg/ebbdvSCXqu). By
contributing you agree your work is licensed under the repo's MIT license. The brand assets
listed in [LICENSE-BRAND](LICENSE-BRAND) — the design system and the Waku mark — are not
MIT and are not open for reuse outside waku-agent.
