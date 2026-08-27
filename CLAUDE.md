# waku-agent — working conventions

**Waku** — a local-first personal assistant demonstrating the four pillars behind every
serious agent: Harness, Loop, Memory, and Eval/LLM-Ops. It began as a teaching repo you
could read in an afternoon, and it's now growing toward a full open-source assistant (the
next Hermes / OpenClaw). The bar for every change: **clear, honest code a newcomer can
follow** — each pillar legible on its own. The project will get bigger; it must never get
muddier. New scope is welcome when it stays self-contained, tested, and readable; complexity
for its own sake is not.

## Architecture map (file ↔ diagram box)

- `waku/gateway/` — cli, voice (wake word), telegram. Gateways only move text.
- `waku/runtime/session.py` — working memory assembly (SOUL.md + memory + history)
- `waku/loop/agent.py` — THE loop; `loop/models.py` — pluggable providers, 2 wire formats
- `waku/graph/` — engine + node factories + `workflows/` (triage) — opt-in structure
  AROUND the loop (the loop never changes; a graph node can BE a loop turn); every
  failure fails open to the plain loop
- `waku/tools/` — create_event / save_note / send_message (flagship task only)
- `waku/memory/` — semantic (FTS5) / episodic / procedural (SKILL.md) +
  `retrieval_gate.py` (hero 1) + `consolidation.py` (every N exchanges)
- `waku/ops/` — tracing (JSONL + OTel), dashboard (localhost:7777), release_gate,
  `compare_history.py` (the Compare arena's own JSONL scoreboard — never state.db)
- `evals/deterministic/` (0/1, pytest) vs `evals/judge/` (DeepEval, scored) — never mix
- `examples/` — teaching material, not product (see the rule below); one folder per topic
- Runtime state lives in `.waku/` (state.db, calendar.ics, outbox/, traces/) — gitignored

## Rules

- **Be concise.** Sean wants short replies: lead with the answer, cut preamble and
  recap. A few lines beats a wall of text. Expand only when he asks for detail.
- **Start every session by draining the community queue.** This is a public repo
  with contributors waiting; an unanswered PR teaches someone that doing what we
  asked gets silence. So on the first substantive turn of a new session, before
  anything else, run:

  ```
  gh pr list --state open  ·  gh issue list --state open
  ```

  Report it as a **short table** — number, title, author, size, CI state, age —
  plus anything already visibly wrong (an unrelated lockfile, a "Closes #N" that
  points at the wrong issue, a `BEHIND` branch). Then propose an order,
  **smallest first**, and stop.

  Then walk the queue **one item at a time**, using the `review-pr` skill's four
  fixed sections, in this order and no other:

  1. **What this is** — plain language, no diff dump
  2. **Why this is important** — the concrete failure, not the abstract benefit
  3. **How do I test this** — copy-paste commands, and say what I already ran
  4. **Merge / modify / close — and why** — one recommendation, then stop

  **After each item, stop and wait for Sean's call on that one item.** Never
  batch, never carry one yes forward to the next. Approving a plan is not
  approval to merge anything. Test in throwaway worktrees via the `pr-worktree`
  skill — never `gh pr checkout`. Frontend and TUI diffs are **Sean's to test**:
  stand them up on port 7778 so the live 7777 is untouched, hand him the URL,
  and never merge on my own screenshots.
- **Never wipe runtime data without asking first, every time.** `scripts/demo_seed.py`
  and anything else that clears `.waku` (memory, calendar, chat log, traces, or the
  `usage.jsonl` spend ledger) must be proposed and explicitly approved by the user
  *immediately before each run*. Permission never carries over from a previous run.
  The script backs up first, but restoring is a hassle — ask, wait for a clear yes,
  then run. It refuses to do anything without the `--yes` flag for this reason.
- **Commit messages are about the CODE, not the conversation.** Subject = what
  changed, under ~70 chars. Body = why, in a few tight lines. Then stop.
  No narrating who asked for it, no "Sean caught", no story of what I tried
  first, no re-deriving the reasoning. This is a public repo — a stranger
  reading `git log` wants the change, not a diary. If the reasoning is worth
  keeping, it belongs in a code comment next to the code it explains.
- **Version control — commit AND ship every milestone, same turn.** The moment a change
  works (tests pass / verified live), commit it and get it onto GitHub before moving on. Never end a
  turn or session with working changes left uncommitted — the repo must always be traceable
  from GitHub, and uncommitted work has been lost to branch switches before. Use the `/ship`
  skill. If several milestones land in one session, commit each as its own logical commit.
- **`main` is protected — `git push origin main` is REJECTED, for everyone.** Since
  2026-07-26 a commit only lands once `skills-and-evals` is green, and `enforce_admins`
  is on, so the rule binds Sean and Claude identically. Ship via
  `git checkout -b <topic>` → `gh pr create --fill` → `gh pr checks --watch` (~30s) →
  `gh pr merge --squash --delete-branch`. `GH006: Protected branch update failed` is the
  guard working; never route around it. Merging a COMMUNITY PR still needs Sean's
  explicit per-PR yes (see `.claude/skills/review-pr/SKILL.md`).
- **Gate before push**: `make gate` (deterministic must pass; judge runs with a key).
  When a live bug is found, fix it AND add a regression case to `evals/deterministic/`.
- **No emojis** in any UI surface (dashboard, CLI output, README prose).
- **No new dependencies without discussion** — the core is stdlib + anthropic/openai.
  Optional features go behind extras (`[voice]`, `[telegram]`, ...).
- **Footprint ladder — where new capability goes.** Every registered tool ships in
  every prompt, so the core stays narrow and capability lives at the edges. In order:
  extend existing code → a skill (`SKILL.md`, no Python) → a CLI + README →
  a tool behind an extra → a gateway (one file, text in/out only) →
  **a new core tool, last resort**. Full version, with the "declined even when
  well-built" list, in `CONTRIBUTING.md`.
- **`examples/` is teaching material, not product.** Video companions, minimal agents,
  and other people's tools shown on their own terms all live here — one self-contained
  folder or file per TOPIC, named for the topic (`memory-native/`), never for the video
  or its date. Four rules keep it from rotting the core:
  1. **Nothing under `waku/` may import from `examples/`.** One-way, always. This is
     the load-bearing rule; the other three are hygiene.
  2. **No new default dependencies.** Use stdlib, or an extra that already exists, or
     state the `pip install` in the file's own header.
  3. **`make gate` must never depend on an example.** A third-party SDK shipping a
     breaking release is their problem, not red CI.
  4. **Anything using someone else's SDK carries a dated header** naming the version it
     was verified against. mem0/zep/langmem move fast, and a silently rotted example is
     worse than no example.
  Whether it imports waku is NOT the test. `tiny_memory_agent.py` imports plenty of it
  (that's the point — the loop's three steps with nothing else in frame);
  `memory-native/` imports none of it (also the point — mem0, Zep, LangMem and pgvector
  the way you'd actually start with them, before any comparison is drawn). The test is
  whether a stranger can run it in one command and learn exactly one thing.
- **Scope**: scheduling is the flagship teaching task, but the project is growing toward a
  full assistant. New capabilities (providers, tools, gateways, integrations) are welcome
  when they're self-contained, tested, and keep the core legible. Reject only complexity
  that muddies how the system works or bloats the default path — prefer opt-in extras.
- Providers are framed neutrally in docs (Anthropic, OpenAI, Gemini, DeepSeek, Kimi, GLM,
  OpenRouter) — no ranking, no "open-source vs closed" framing.

## Commands

`make run` · `make voice` · `make dashboard` (7777) · `make trace` (6006) ·
`make eval` · `make gate` · `make lint` · tests live under `evals/`, not `tests/`
