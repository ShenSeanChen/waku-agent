# docs — what is in here

Start with [AGENTS.md](../AGENTS.md) at the repo root: it routes every kind of
change to the one file that covers it. This folder holds those files, sorted
into four groups.

## Start here

[status.md](status.md) — what works, what is known-broken, what is
deliberately not built. Rewritten whole rather than appended to, so it
cannot become a changelog. Read it before opening a PR: most of what is
already known-broken is listed, and half of it has a fix in flight.

## The rulebook

| file | what it answers |
|---|---|
| [context/conventions.md](context/conventions.md) | how much process a change needs, where capability goes, testing, git, scope |
| [context/design-system.md](context/design-system.md) | how the dashboard looks, and which primitive to use |
| [context/writing-rules.md](context/writing-rules.md) | how we write docs, UI copy and commit messages |
| [context/gotchas.md](context/gotchas.md) | traps someone already stepped on |
| [context/maintainers.md](context/maintainers.md) | how maintainers review, merge and release; contributors can skip it |

## Reference — how the system works

| file | what it answers |
|---|---|
| [architecture.md](architecture.md) | the four pillars, and which file is which diagram box |
| [loop-vs-graph.md](loop-vs-graph.md) | when a turn needs shape, and why the loop never changes |
| [agent-graphs-design.md](agent-graphs-design.md) | the graph engine's design and its fail-open rule |
| [memory-backends-playbook.md](memory-backends-playbook.md) | seeing your memories in each provider's own console |
| [benchmarks.md](benchmarks.md) | what has been measured, and how |
| [integrations.md](integrations.md) | voice, Telegram, Apple, Google Calendar, MCP — all opt-in |

## Whiteboards

[whiteboards/](whiteboards/) holds the editable `.excalidraw` boards that
explain Waku. `scripts/whiteboard/build_*.py` generates them so they match the
hand-drawn masters; edit the builder, not the JSON.

## Deliberately not in docs/

- [../examples/](../examples/README.md) — short runnable lessons about Waku
  itself.
- [../lab/](../lab/README.md) — one folder per outside topic (Kimi K3, pi, the
  memory products), with its write-up, boards and code. Video work starts
  there.

[conventions §6](context/conventions.md#6-examples-and-video-material) has the
rules for both.
