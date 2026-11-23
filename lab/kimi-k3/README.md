# Kimi K3

Kimi K3 is a sparse mixture-of-experts model. This topic explains how it works
for someone with a statistics background, and races it against other models on
Waku's own tasks.

## The question

What makes Kimi K3 fast and cheap, and how does it do on Waku's own tasks?

## What we connect

The Waku agent's model shootout runs the same tasks through K3 and another
model. Kimi is an ordinary provider row in `waku/loop/models.py`.

Verified against: Kimi K3 through the Kimi API, 2026-07-24

## Run it

```bash
make shootout RUNS="kimi:kimi-k3 anthropic:claude-opus-4-8"
```

The shootout needs an API key for each provider in `.env`.

## What we found

[write-up.md](write-up.md) explains the architecture (the 16-of-896 experts,
KDA and AttnRes attention, and why agent loops get cheap) and what the arena
found. [docs/benchmarks.md](../../docs/benchmarks.md) has the measured runs.

## Video angle

- **Hook:** why an agent loop gets cheap on K3, explained with statistics you
  already know.
- **Board:** [whiteboards/k3-architecture.excalidraw](whiteboards/k3-architecture.excalidraw).
  `build_k3_tutorial.py` draws the two-board tutorial version into `whiteboards/`.

## Graduation

Already in the product: Kimi is a provider row in `waku/loop/models.py`, and
the shootout is `scripts/shootout.py`. The write-up and the boards stay here.
