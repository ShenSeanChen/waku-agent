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
- `waku/tools/` — create_event / save_note / send_message (flagship task only)
- `waku/memory/` — semantic (FTS5) / episodic / procedural (SKILL.md) +
  `retrieval_gate.py` (hero 1) + `consolidation.py` (every N exchanges)
- `waku/ops/` — tracing (JSONL + OTel), dashboard (localhost:7777), release_gate,
  `compare_history.py` (the Compare arena's own JSONL scoreboard — never state.db)
- `evals/deterministic/` (0/1, pytest) vs `evals/judge/` (DeepEval, scored) — never mix
- Runtime state lives in `.waku/` (state.db, calendar.ics, outbox/, traces/) — gitignored

## Rules

- **Be concise.** Sean wants short replies: lead with the answer, cut preamble and
  recap. A few lines beats a wall of text. Expand only when he asks for detail.
- **Never wipe runtime data without asking first, every time.** `scripts/demo_seed.py`
  and anything else that clears `.waku` (memory, calendar, chat log, traces, or the
  `usage.jsonl` spend ledger) must be proposed and explicitly approved by the user
  *immediately before each run*. Permission never carries over from a previous run.
  The script backs up first, but restoring is a hassle — ask, wait for a clear yes,
  then run. It refuses to do anything without the `--yes` flag for this reason.
- **Version control — commit AND ship every milestone, same turn.** The moment a change
  works (tests pass / verified live), commit it with a detailed message (subject = what,
  body = WHY + what it survived) and get it onto GitHub before moving on. Never end a
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
- **Scope**: scheduling is the flagship teaching task, but the project is growing toward a
  full assistant. New capabilities (providers, tools, gateways, integrations) are welcome
  when they're self-contained, tested, and keep the core legible. Reject only complexity
  that muddies how the system works or bloats the default path — prefer opt-in extras.
- Providers are framed neutrally in docs (Anthropic, OpenAI, Gemini, DeepSeek, Kimi, GLM,
  OpenRouter) — no ranking, no "open-source vs closed" framing.

## Commands

`make run` · `make voice` · `make dashboard` (7777) · `make trace` (6006) ·
`make eval` · `make gate` · `make lint` · tests live under `evals/`, not `tests/`
