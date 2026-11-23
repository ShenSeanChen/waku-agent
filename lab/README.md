# lab — one folder per outside topic

The lab is where Waku meets other projects: another agent, a model or a memory
product, and how Waku's agent, memory and skills connect to it. Video work
starts here. None of it is the product: nothing under `waku/` or `evals/`
imports from `lab/`, and `lab/` never ships to PyPI.

| Topic | The question |
|---|---|
| [kimi-k3/](kimi-k3/README.md) | What makes Kimi K3 fast and cheap, and how does it do on Waku's own tasks? |
| [pi-agent/](pi-agent/README.md) | Who owns an agent's context: the vendor, or you? |
| [memory-native/](memory-native/README.md) | What do mem0, Zep, LangMem and Supabase do on their own terms, before any comparison with Waku? |
| [one-memory-every-agent/](one-memory-every-agent/README.md) | Can one memory follow you across Grok Bot, Muse, Claude Code, Codex and the Waku agent? |

## Start a topic

1. Copy [_template/README.md](_template/README.md) to `lab/<topic>/README.md`.
   Name the folder for the topic, never for a video or a date.
2. Fill in all six sections and keep their headings.
   `evals/deterministic/test_rulebook.py` checks the headings and the
   `Verified against:` line.
3. Keep the topic's code and any long write-up (`write-up.md`) in the same
   folder. Boards go in as PNG screenshots, in `screenshots/`; their editable
   `.excalidraw` sources and the scripts that draw them stay out of the repo.

The rules, including when lab code graduates into `waku/`, are in
[conventions §6](../docs/context/conventions.md#6-examples-and-video-material).
