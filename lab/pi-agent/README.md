# pi

pi is a small open-source coding agent. This topic walks through how it is
built, and how the Waku agent hands coding work to it.

## The question

Who owns an agent's context: the vendor, or you?

## What we connect

The Waku agent delegates a coding job to pi through its `delegate_task` tool.
[pokedex/](pokedex/README.md) is a small pi package, a skill plus an extension,
that shows how pi loads tools you wrote yourself.

Verified against: pi 0.82.0, 2026-07-25

## Run it

```bash
pi -e ./lab/pi-agent/pokedex -p "What is super-effective against Charizard?"
```

## What we found

[write-up.md](write-up.md) is the full walkthrough: every box on the chart,
where it lives on disk, and what each demo printed.

## Video angle

- **Hook:** who owns the context, the vendor or you? pi's answer is that you do.
- **Boards:** [whiteboards/pi-architecture.excalidraw](whiteboards/pi-architecture.excalidraw),
  [whiteboards/pi-system-design.excalidraw](whiteboards/pi-system-design.excalidraw)
  (drawn by `build_pi_system.py`) and
  [whiteboards/pi-vs-claude-code.excalidraw](whiteboards/pi-vs-claude-code.excalidraw)
  (drawn by `build_pi_vs_claude.py`). The chart prompt is
  [whiteboards/pi-chart-prompt.md](whiteboards/pi-chart-prompt.md).

## Graduation

Already graduated: `delegate_task` hands a coding job to pi (see "Sub-Agents"
in the README). The Pokédex package stays a demo.
