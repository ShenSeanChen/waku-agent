# Project Harness — reference spec

> Status: **proposal** (not yet implemented). This document defines the reusable
> schema for instantiating a project harness from the CTO kernel. It authorizes no
> build; it is the shape every later piece is built against.

## 1. The split: one kernel, many harnesses

The core discipline, stated once so it binds everywhere else:

- **The kernel** is *code* — project-agnostic capability. One kernel serves every
  project. Nothing project-specific may enter it.
- **A project harness** is *data + config + project skills* — the institutional
  brain and technical management layer for one project. It instantiates the kernel
  against one project's world.

The kernel today is the Waku fork: the loop (`loop/agent.py`), the graph engine
(`graph/`), memory (semantic / episodic / procedural), skills (`SKILL.md`),
sub-agent delegation (`delegate_task` → pi / Claude Code / Codex), the
deliberation workflow (`waku deliberate`, bounded to N rounds), evals and traces.
All of it is already project-agnostic; the rule is only that it stays that way.

**Five businesses = one kernel + five knowledge maps.** Not five agent systems.

## 2. What a project harness is

> **Qamata Harness = everything a competent CTO would need to know, decide,
> inspect, delegate, challenge, and remember about Qamata.**

It is **a map of knowledge, not a copy of it.** The project's files stay where the
project put them. The harness indexes them — where each thing lives, what authority
it has, how it relates, when it was last verified, and who may change it.

So the harness does not force the project into an agent framework; it accretes
*around* the project. For Qamata, `PLAN.md`, `PROGRESS.md`, `architecture/`,
`decisions/` and `reviews/` stay exactly where they are; the map points into them.

The harness also holds the project's **team** — its roles and their role-scoped
skills — defined as data, not kernel code. See [docs/project-roles.md](project-roles.md).

## 3. `project.toml` — the schema

One file at the harness root. Identity, then a map, then the controls. TOML,
parsed by stdlib `tomllib` (Python ≥3.11) — so the harness config carries no new
dependency.

```toml
name = "qamata"
title = "Qamata"
owner = "Proofs Africa"
thesis = "Governed, deterministic compute for agents and their actions."
kernel = "compatible-any"   # a kernel version the harness requires
created = "2026-09-14"
updated = "2026-09-14"

[authority]                # the ladder, see §5
default = "recommend"
irreversible = ["settle", "prod_deploy", "push_main"]

# [[map]] blocks define the knowledge map, see §4
# [[watches]] blocks define assumptions to monitor, see §6
# [[roles]] blocks define the team, see docs/project-roles.md
```

The directory skeleton from `new-project` (§7) is a **default for greenfield
projects only**. For an existing project the map points at the project's own
layout — the map is the schema, not the folders.

## 4. The knowledge map

Each entry is one addressable item of consequential knowledge.

| Field | Meaning |
|---|---|
| `id` | stable address, e.g. `qamata.arch.isolation` |
| `kind` | `doctrine` · `product` · `architecture` · `engineering` · `operations` · `decision` · `intelligence` · `assumption` · `learning` |
| `path` | where the content lives (file, section, URL, git ref, or `none` for a recorded claim) |
| `authority` | `source-of-truth` · `working-model` · `decided` · `proposal` · `history` |
| `status` | `established` · `hypothesis` · `open` · `deferred` · `stale` |
| `owner` | who may change it: `product-owner` · `architect` · `maintainer` · `harness` |
| `depends_on` / `depended_on_by` | the graph edges — this is what makes "which documents rely on this assumption?" answerable |
| `verified` | when a human or the harness last confirmed the claim holds |
| `freshness` / `watch` | how it goes stale: on-change, after N days, or when a metric crosses a threshold |

Two rules make it a map and not a dump:

1. **Authority is explicit.** `PLAN.md` is `source-of-truth`; `architecture/v1-scope.md`
   is `proposal` (not authorized); `deprecated/v1` is `history` (no standing
   authority). The harness must never let a `proposal` silently override a
   `source-of-truth` — the same rule Qamata already states in its own plan.
2. **The dependency graph is load-bearing.** `depends_on` / `depended_on_by` are
   not documentation; they are queried at runtime. When an assumption is falsified,
   the harness walks the graph to find everything downstream of it.

## 5. The authority ladder

A CTO does not execute everything indiscriminately; neither does the harness.
Each rung maps to tool access, and the ladder is **enforced in the tool gate**,
not stated in a prompt.

| Rung | What it covers | Mechanism |
|---|---|---|
| Observe | read code/docs, `search_web`, query telemetry | always-on |
| Analyse | reason over observations | always-on |
| Recommend | propose a course of action | always-on |
| Plan | draft a plan, a PRD, an ADR | always-on (writes only into harness/sandbox dirs) |
| Prepare | stage changes, create a branch | always-on (non-destructive) |
| Execute in sandbox | `delegate_task`, `run_command` in a sandbox | on, sandboxed (`workspace-write` / `read-only`) |
| Execute reversible | deploy to staging, open a PR | gated: requires a recorded decision |
| Execute irreversible | `push_main`, prod deploy, `settle`, customer contact | owner approval, evidence required |

The farther right, the more evidence and the higher the authorization. For a
one-person company this is *more* important, not less — the harness is the missing
"are you sure?" counterweight. `authority.irreversible` in `project.toml` names
the tools that can never run without the owner.

## 6. The learning loop

The harness is not `prompt → tools → answer`. It is:

> **knowledge → plan → act → observe → verify → record → reconsider → improve**

Three steps already exist in the kernel: **observe** (traces, OTel), **verify**
(evals), **record** (memory consolidation, the usage ledger). Two are new:

- **Reconsider** — when an observation falsifies a recorded claim, the harness
  does not merely log a metric. It queries the map for `depended_on_by`, marks
  those items `stale`, and proposes an experiment.
- **Improve** — apply the learning: update the map, update the affected ADR or
  skill, and feed the eval loop.

Worked example — the Qamata placement assumption:

```toml
[[watches]]
id = "qamata.assumption.placement-startup"
kind = "assumption"
authority = "hypothesis"
status = "hypothesis"
claim = "Provider A starts a workload in ~18s"
metric = "placement.startup_median"   # what to observe
op = "lt"                             # the bound that keeps the claim true
threshold = 25.0                      # crossing it falsifies the claim
depended_on_by = ["qamata.arch.placement", "qamata.arch.tariff", "qamata.ops.runbook-coldstart"]
```

When the observatory sees the median drift to 41s, `reconsider` evaluates the
watch (`41 < 25` is false), fires it, and reasons — *our prior placement
assumptions are invalid; this affects Placement; three documents depend on the
old number; run an experiment to decide temporary vs structural* — naming the
dependent items to mark `stale`. That is CTO behaviour, not a metrics dashboard.
(`waku reconsider` reports; the `stale` marking is a deliberate owner step.)

## 7. Lifecycle

**Bootstrapping a serious project** no longer starts `mkdir && git init`; it starts
`new-project qamata`, which stamps the skeleton from this spec. The first
conversations are, in order: *understand this project → build its model →
identify what we don't know → establish architectural and operational controls* —
and only then substantial autonomous execution.

**`bring me back`** reconstructs state from the map: what I was trying to
accomplish, where I left it, what changed while I was away, which decisions are
still unresolved, and the three most consequential next considerations.

**`status`** renders the map against freshness: which items are stale, which
watches are firing, which proposals are waiting on a decision.

## 8. Qamata — worked example

A partial map of the project as it exists today (files are real; the map is the
new layer on top):

```toml
[[map]]
id = "qamata.doctrine"
kind = "doctrine"
path = "PLAN.md"
authority = "source-of-truth"
status = "established"
owner = "product-owner"
depended_on_by = ["qamata.arch.input-brief", "qamata.sc01"]

[[map]]
id = "qamata.progress"
kind = "operations"
path = "PROGRESS.md"
authority = "source-of-truth"
status = "established"
owner = "maintainer"
freshness = "update-on-handoff"

[[map]]
id = "qamata.arch.input-brief"
kind = "architecture"
path = "architecture/input-brief.md"
authority = "working-model"        # subordinate to PLAN.md
status = "established"
owner = "architect"
depends_on = ["qamata.doctrine"]

[[map]]
id = "qamata.sc01"
kind = "decision"
path = "decisions/SC-01-first-workflow-and-offer.md"
authority = "decided"
status = "established"
owner = "product-owner"
depends_on = ["qamata.doctrine"]
depended_on_by = ["qamata.arch.v1-scope"]

[[map]]
id = "qamata.arch.v1-scope"
kind = "architecture"
path = "architecture/v1-scope.md"
authority = "proposal"
status = "deferred"                  # not authorized
owner = "architect"
depends_on = ["qamata.sc01"]

[[map]]
id = "qamata.arch.isolation"
kind = "architecture"
path = "none"                       # an open question, not yet an ADR
authority = "proposal"
status = "open"
note = "VM-per-run vs container+gVisor/Firecracker — AR-01, deferred to the v1 build authorization"
```

## 9. Build order

1. **This spec** — the shape everything else is built against. ✔
2. **`new-project`** — stamp a harness from the spec; seed the map by indexing the
   project's existing docs (Qamata first).
3. **`bring me back`** — the reconstruction briefing from the map.
4. **The authority gate** — the ladder enforced over the tool registry.
5. **`reconsider`** — the watch → mark-stale → propose-experiment step.

The rule from here on: **the kernel stays generic; the harness is where a project
lives.** Qamata's harness is the reference implementation the others are stamped
from.
