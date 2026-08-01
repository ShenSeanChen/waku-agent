---
name: new-tool
description: Add a new tool to Milli the right way — schema, safe execution, deterministic eval, honest output. Use when adding or modifying agent tools or when the user asks for a new capability.
---

## Procedure

1. One file per tool in `milli/tools/`, exposing `make_tool(...) -> Tool`.
   Copy the shape of `milli/tools/calendar.py` (the reference implementation).
2. The tool function must be:
   - **Idempotent** where repeat calls could occur (check-before-insert, like
     create_event's title+start guard).
   - **Honest in its return string**: say exactly where the artifact went
     (file path, table) and how the user can see it. Never imply effects the
     tool doesn't have.
   - **Local-first by default**: write to `.milli/` or state.db; real external
     services go behind env-gated adapters.
3. Register it in `milli/tools/__init__.py:build_registry`.
4. Add deterministic eval coverage in `evals/deterministic/`:
   - offline: scripted client fires the tool → assert the artifact (DB row/file)
   - a dataset case in `evals/dataset.jsonl` for the live tier
5. If the tool needs usage guidance, add a rule to DEFAULT_SOUL
   (`milli/runtime/session.py`) or a skill in `skills/`.
6. Check scope first: the repo ships flagship-task tools only. New capabilities
   beyond scheduling/notes/messages should be discussed before building.
