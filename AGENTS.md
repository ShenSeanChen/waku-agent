# AGENTS.md — read this first

Waku is a local-first personal assistant that shows the four pillars behind
every serious agent: Harness, Loop, Memory, and Eval/LLM-Ops. It began as a
repo you could read in an afternoon and is growing into a full open-source
assistant. Every change must keep it **clear, honest code a newcomer can
follow**: the project will get bigger, and it must never get muddier.

This file is for everyone who changes the repo, human or coding agent. A test
caps it at 100 lines, so the detail lives in the files it points to.

## Before you… read…

| Before you | Read |
|---|---|
| run Waku for the first time | [docs/getting-started.md](docs/getting-started.md) |
| start anything non-trivial | [docs/status.md](docs/status.md): what works and what is known-broken |
| decide how much process a change needs | [conventions §2](docs/context/conventions.md#2-how-much-process-a-change-needs) |
| add capability (tool, gateway, provider, store) | [conventions §3](docs/context/conventions.md#3-where-new-capability-goes-the-footprint-ladder), the footprint ladder |
| add a skill | [CONTRIBUTING.md](CONTRIBUTING.md): no Python needed |
| connect Waku Memory, or carry skills to another agent | [docs/integrations.md](docs/integrations.md#share-one-memory-with-your-other-agents-waku-memory) |
| add a tool | the `new-tool` skill in `.claude/skills/new-tool/` |
| touch the loop, memory, graph engine or a tool contract | [docs/architecture.md](docs/architecture.md), then conventions §2: it may need a proposal |
| change how the dashboard looks | [docs/context/design-system.md](docs/context/design-system.md) |
| change dashboard JavaScript or CSS | [waku/ops/static/README.md](waku/ops/static/README.md) |
| write a doc, UI copy, a commit message or a SKILL.md | [docs/context/writing-rules.md](docs/context/writing-rules.md) |
| add a lesson to `examples/` or a topic to `lab/` | [lab/README.md](lab/README.md), then [conventions §6](docs/context/conventions.md#6-examples-and-video-material) |
| hit something surprising | [docs/context/gotchas.md](docs/context/gotchas.md), and add it if it is missing |

## Hard rules

1. **Never wipe runtime data without asking.** Anything that clears `.waku/`
   (memory, calendar, chat log, traces, the `usage.jsonl` spend ledger),
   `scripts/demo_seed.py` included, needs the user's explicit yes right before
   each run. A yes never carries over to the next run.
2. **Never touch secrets.** No hidden network calls, nothing reads or sends
   `.env` or keys, and nothing runs at install time. Waku runs on people's own
   machines with their own keys.
3. **No new default dependency.** The core is stdlib plus the Anthropic and
   OpenAI clients. Anything else goes behind an extra (`[voice]`, `[telegram]`).
4. **Every behaviour change gets a deterministic eval** in `evals/deterministic/`
   (0/1, offline). A bug fix adds the case that would have caught it.
5. **Nothing under `waku/` or `evals/` imports from `examples/` or `lab/`,** and
   `lab/` never ships to PyPI.
6. **No emojis** in the dashboard, CLI output or docs prose.
7. **The Waku brand is not MIT.** The design system, the Waku mark and the names
   are listed in `LICENSE-BRAND`. List any new brand file there, and never copy
   one into `examples/`.
8. **Don't edit the copied design files** in `waku/ops/static/design/`. They are
   synced from the private master; ask for a new token in an issue.
9. **Fix the doc your change makes false,** in the same PR.

## What CI blocks

The `validate` workflow runs on every PR. Each of these fails it:

| Blocked | Checked by |
|---|---|
| a `uv.lock` change without a `pyproject.toml` change | a step in `.github/workflows/validate-skills.yml` |
| a lint error in `waku/`, `evals/` or `scripts/` | `ruff check` |
| a skill that fails validation | `scripts/validate_skills.py` |
| a skill that loads on everyday or another skill's messages | `evals/deterministic/test_skill_triggers.py` |
| `.env.example` out of step with the integrations registry | `scripts/generate_env_example.py` |
| an edited design copy, a colour literal, an old token name | `evals/deterministic/test_design_system.py` |
| a second version number | `evals/deterministic/test_version.py` |
| this file over 100 lines, a broken rulebook link, an unindexed doc, an import from `examples/` or `lab/`, a lab topic without its playbook, a retired Waku Memory address, an emoji in the rulebook or README | `evals/deterministic/test_rulebook.py` |
| any other failing deterministic eval | `pytest evals/deterministic` |

Everything else in the rulebook is checked in review. The judge evals in
`evals/judge/` need an API key, so `make gate` runs them locally and CI does not.

## Commands

`make run` · `make dashboard` (localhost:7777) · `make voice` · `make trace` (Phoenix, 6006)
`make eval` · `make gate` (deterministic + judge) · `make lint` · tests live in `evals/`, not `tests/`

## Maintainers

How maintainers review, merge and release is in
[docs/context/maintainers.md](docs/context/maintainers.md). Contributors can skip it.
