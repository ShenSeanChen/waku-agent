# Project Roles — reference

> Status: **proposal**. Roles are currently *implicit* in the kernel — two hardcoded
> lenses (`_LENSES` in `waku/ops/deliberate.py`), prompt templates, and an env-var
> model map. This document defines them as **harness data**, with role-scoped
> skills, so a project configures its own team without touching kernel code.

## 1. The team

Five agent roles, plus the human. Each owns one question about the work.

| Role | Lens | Runtime | Authority | Owns |
|---|---|---|---|---|
| **Architect** | SOFTWARE ARCHITECT | Claude Code · Fable 5.1 (medium) | `recommend` | boundaries, seams, contracts, trade-offs, ADRs |
| **Reviewer** | PRODUCT AND SYSTEMS REVIEWER | DeepSeek (direct, high) | `recommend` | requirements, risk, operational + cost consequences |
| **Decider** | DECIDER | Codex · Astra (high) | `recommend` | the final call — synthesis, disagreement, one decision/verdict |
| **Engineer** | ENGINEER | Claude Code · Sonnet (or any coding agent) | `sandbox` | the build — code, tests, ship |
| **Tester** | TESTER | the eval gate (deterministic + judge) | `plan` | verification — did it do the thing, and is it any good |

The **Product Owner** is the human — not an agent role. It sits above the ladder,
signs the decisions the team drafts, and holds the top rungs the team never
reaches (`reversible` / `irreversible`).

The division is deliberate: **Architect/Reviewer/Decider think** (authority
`recommend` — they write and advise, never execute); **Engineer acts in a
sandbox**; **Tester verifies**. The authority ladder (§5 of the harness spec)
already encodes this — a role's `authority` is the highest rung it may touch.

## 2. How roles map to the primitives

A role is **not a new primitive** — it is a named configuration over the ones
that already exist:

> **A role = { node kind (`tool` / `llm` / `agent`), brain (model + effort),
> skills (loaded by id), authority ceiling (the tool gate). }**

The graph already has the node factories (`tool_node`, `llm_node`, `agent_node`);
the loop already has `agent_node` = a full `run_loop` turn. A role only names
*which one to use, with which brain, skills, and ceiling*:

| `runtime` | The primitive it maps to | Is this the loop? |
|---|---|---|
| `claude` · `codex` · `pi` | a `tool` node calling `delegate_task` — a subprocess to a coding agent | no — a subprocess, not `run_loop` |
| `deepseek` | an `llm` node — one bare `messages.create`, no tools | no — a single call, not a loop turn |
| `loop` | an `agent` node — a full `run_loop` turn | **yes** — THE loop |
| `eval` | the eval gate (`release_gate.py`) | no — pytest, outside loop and graph |

So most of the team does **not** run as loop turns: Architect/Engineer/Decider
are subprocess calls, Reviewer is a bare model call, Tester is the eval gate.
The loop is the **orchestrator + fail-open**, plus any role with `runtime: loop`.
That is a deliberate choice — the heavy-reasoning roles are external coding
agents — and it is faithful to the *graph* (a node may be `tool`/`llm`/`agent`)
rather than to the *loop*, for those roles.

## 3. The `[[roles]]` schema

One `[[roles]]` block per role in `project.toml`.

| Field | Meaning |
|---|---|
| `id` | stable name, e.g. `architect` |
| `name` | human-facing, e.g. `Software Architect` |
| `lens` | how the prompt addresses it |
| `responsibility` | one line: what this role owns |
| `runtime` | the primitive it runs on — see §2: `claude` · `codex` · `pi` (a `tool` node → `delegate_task`) · `deepseek` (an `llm` node) · `loop` (an `agent` node) · `eval` (the eval gate) |
| `model` | the brain it runs on (the caller's own id) |
| `effort` | optional reasoning effort (`low`/`medium`/`high`/…) |
| `authority` | the highest rung its tools may reach — gates what the role may *do* |
| `skills` | its procedures, by id — loaded only into this role's context |

```toml
[[roles]]
id = "architect"
name = "Software Architect"
lens = "SOFTWARE ARCHITECT"
responsibility = "boundaries, seams, contracts, trade-offs, ADRs"
runtime = "claude"           # a tool node → delegate_task, not the loop
model = "fable-5.1"
effort = "medium"
authority = "recommend"
skills = ["write-adr", "architecture-review", "dependency-audit"]
```

## 4. Role-scoped skills

Skills are procedural memory: *how* a role acts. They are the answer to "what
would a competent person in this role *do*", captured as `SKILL.md` files. They
live in the harness, scoped per role:

```
harness/
  skills/
    architect/
      write-adr/SKILL.md
      architecture-review/SKILL.md
      dependency-audit/SKILL.md
    reviewer/
      requirements-review/SKILL.md
      risk-analysis/SKILL.md
    decider/
      decision-synthesis/SKILL.md
    engineer/
      test-first/SKILL.md
      surgical-change/SKILL.md
      debugging/SKILL.md
    tester/
      deterministic-tests/SKILL.md
      eval-design/SKILL.md
      regression-hunting/SKILL.md
```

Each skill is a narrow, trigger-worded procedure (the existing `SKILL.md`
format). A role's brain loads **only its own** skills — never the whole pool:

- **Architect** — `write-adr` (record a decision: context, options, trade-offs,
  decision, consequences) · `architecture-review` (a diff/design for boundaries,
  seams, coupling, layering) · `dependency-audit` (map dependencies; flag cycles
  and wrong-direction edges).
- **Reviewer** — `requirements-review` (hold the work against requirements; name
  unmet or over-promised commitments) · `risk-analysis` (failure modes and
  operational/cost consequences; separate temporary from structural).
- **Decider** — `decision-synthesis` (merge two positions, name agreement vs
  disagreement, issue one call — no "on the other hand" without a side).
- **Engineer** — `test-first` (the failing test before the fix) ·
  `surgical-change` (minimal diff, match conventions, no speculative scope) ·
  `debugging` (reproduce → isolate → root-cause before fixing).
- **Tester** — `deterministic-tests` (0/1 assertions; no model judges "did it do
  the thing") · `eval-design` (keep deterministic and judged suites separate) ·
  `regression-hunting` (turn a live bug into a deterministic case that can't return).

These are distinct from the kernel's bundled `skills/` (general assistant
procedures) and from `.claude/skills/` (the *coding agent's* own build-loop
skills) — role skills are the project team's procedures, stored as project data.

**How they load — by id, not by keyword-match.** The role names its skills
explicitly (`skills = [...]`), so there is nothing to match. Reused: the
`SKILL.md` format and its parser (`_parse_text` in
`waku/memory/procedural/loader.py`). **Not** reused: `SkillLoader.match()` —
that path is keyword-match-against-message into the *loop's* prompt, which serves
the plain assistant, not a role's named set. Injection depends on the runtime:

- `loop` / `deepseek` — prepend the role's skills to its prompt (the same shape
  as the loop's "Relevant skill instructions" block).
- `claude` / `codex` / `pi` — hand them to the coding agent via its skill flag
  (`--skill` for claude/pi; codex resolves its own skills), or prepend to the task.

## 5. Binding roles to the machinery

| Mechanism | Role binding |
|---|---|
| **Deliberation graph** | `brain_a` → architect · `brain_b` → reviewer · `decide` → decider. They run as `tool` / `llm` / `tool` nodes (not loop turns); review mode keeps the same three but prompts "verdict" instead of "decision". |
| **Build & review (S9)** | `engineer` builds (Sonnet via `delegate_task`), then the deliberation graph reviews in review mode. |
| **Evaluation gate** | `tester` → `evals/deterministic` (0/1) + `evals/judge` (scored) + `make gate`. |
| **Authority ladder** | a role's `authority` is its ceiling; the tool gate refuses anything above it, and `irreversible` tools are the Product Owner's alone. |

The rewire this implies: `waku/ops/deliberate.py` stops hardcoding `_LENSES` and
the model env-vars, and instead reads `[[roles]]` from the bound harness — the
role's `lens` seeds the prompt, its `skills` join its context, and its
`authority` gates its tools. This is what closes the "roles are implicit in the
kernel" gap.

## 6. Qamata — worked example

```toml
[[roles]]
id = "architect"
name = "Software Architect"
lens = "SOFTWARE ARCHITECT"
responsibility = "Qamata's boundaries: intake, policy, placement, execution, settlement — and the isolation primitive (AR-01)"
runtime = "claude"
model = "fable-5.1"
effort = "medium"
authority = "recommend"
skills = ["write-adr", "architecture-review", "dependency-audit"]

[[roles]]
id = "reviewer"
name = "Product and Systems Reviewer"
lens = "PRODUCT AND SYSTEMS REVIEWER"
responsibility = "Qamata's requirements and risks: deterministic core, bounded spend, isolation, metering, evidence"
runtime = "deepseek"
model = "deepseek-v4-pro"
authority = "recommend"
skills = ["requirements-review", "risk-analysis"]

[[roles]]
id = "decider"
name = "Decider"
lens = "DECIDER"
responsibility = "one reconciled call per decision — e.g. the AR-01 isolation choice"
runtime = "codex"
model = "astra"
effort = "high"
authority = "recommend"
skills = ["decision-synthesis"]

[[roles]]
id = "engineer"
name = "Engineer"
lens = "ENGINEER"
responsibility = "the v1 coding-agent sandbox build, in a sandbox, against the deterministic core"
runtime = "claude"
model = "sonnet"
authority = "sandbox"
skills = ["test-first", "surgical-change", "debugging"]

[[roles]]
id = "tester"
name = "Tester"
lens = "TESTER"
responsibility = "the release gate: deterministic admission/settlement rules + judged quality"
runtime = "eval"
authority = "plan"
skills = ["deterministic-tests", "eval-design", "regression-hunting"]
```

## 7. Build order

1. **This doc** — the shape every later piece is built against. ✔
2. **`[[roles]]` in the harness** — extend `project.toml` (and `new-project`'s
   template) with the roles block + a `skills/<role>/` skeleton.
3. **Rewire `deliberate.py`** — consume `[[roles]]` (lens + model + skills)
   instead of `_LENSES` and the env-var map.
4. **Load role skills** — a role's `skills` (by id) join that brain's context:
   prepended to the prompt for `loop`/`deepseek`, passed via `--skill` for the
   coding agents.
5. **Bind `role.authority`** — a role's tools are gated to its rung.
