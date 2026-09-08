# Research UI integration — minimum-change proposal

**Status: proposal only. Existing dashboard/frontend files are NOT changed by this PR.**
The command integration fix is implemented; this document identifies the smallest
honest read-only monitoring extension. It does not authorize a live campaign.

## Implemented integration fix

`waku.ops.commands.discover()` registers a workflow when its ops module exposes
`run_<name>`. Both `commands.run()` and `dashboard.graph_stream()` invoke that
runner with only an observer (and optionally a chat message). Research previously
required positional campaign/workers, so discovery advertised a command that
raised `TypeError`.

The research adapter now accepts the existing invocation contract. Without explicit
Python campaign/worker bindings it returns `BLOCKED`, no report and a readable
`digest`. Chat text is not converted into a campaign, and no default clients,
fetchers, workers, model calls or writes are introduced. This uses the native
extension point rather than modifying shared command dispatch.

The code marks the intentional boundary:

```python
# ponytail: reuse slash/SSE discovery; no live binder until start approval exists.
```

Four regression cases cover empty/argument-bearing slash commands and the existing
chat/graph-stream surfaces. They failed on the actual missing-arguments exception
before the fix; no existing tests were modified.

## Why the current extensions do not yet provide a research monitor

| Verified seam | Current behavior | Consequence |
| --- | --- | --- |
| `waku/ops/dashboard.py:427-489` | `/api/data` lists only triage/gather topologies | An additive workflow module alone cannot appear as a dashboard chart. |
| `waku/ops/static/js/views.js:455-491` | Generic charts already iterate `g.workflows`; descriptions and runner are triage/gather-specific | Reuse chart iteration; add research semantics, not a new page framework. |
| `waku/ops/static/js/graph.js:28-75` | `graphSVG()` already draws `Graph.describe()` | No new graph library or hand-maintained diagram. |
| `waku/ops/static/js/graph.js:48` | `tool` means “code, no model”; `llm` means “one model call” | Research injected workers can make zero or multiple calls. Use neutral research worker kinds, without relabeling existing workflows. |
| `waku/ops/dashboard.py:858-880` | `/api/events` exposes events from the configured daily trace file | Explicit `Tracer.event` opt-in can reuse the existing transport; no WebSocket service needed. |
| `waku/ops/static/js/diagram.js:133-149` | Trace animation staggers events rather than applying wall-clock live state | Chart animation is not an authoritative timing/parallelism monitor. |
| `waku/ops/static/js/graph.js:201-234` | One global runner state; `graph_end` ends activity, `done` only understands digest/error | Do not equate a drained graph with research publication PASS, or mix two run IDs. |
| `waku/ops/static/js/render.js:169-173` | Chat route labels reduce to quick/full | Do not reuse this label as a research verdict. |

The existing observer transports events but the default research adapter creates
no tracer. Therefore visibility needs explicit metadata tracing to the monitored
home. Reading a dashboard must not implicitly start or bind research.

A dry Node.js check fed the real `describe()` output into the unchanged
`graphSVG()`: all 29 nodes and 44 edges rendered, with zero worker calls. This
proves renderer reuse, not a running dashboard or visual acceptance.

## Recommended scope: read-only topology plus latest-run status

Start with the existing Graph tab and `/api/data` polling. Keep execution and
publication approval separate. Do NOT add a Run research button or wire the
current unconfigured command to a live worker provider.

### New research files: additive implementation hooks

1. `waku/graph/workflows/research.py`: add a dry `research_topology()` derived from
   the real builder, with inert bindings that must never be invoked. Represent
   the supported maximum bounded topology explicitly; mark untaken branches as
   inactive, not indefinitely queued. Use a neutral worker kind for injected
   research callables so the existing renderer makes no false model-call claim.
2. `waku/ops/research.py`: add a metadata-only terminal publication event AFTER
   audit/publication checks (not merely at engine `graph_end`). Include workflow,
   campaign/run ID, PASS/BLOCKED, score/threshold and sanitized error categories;
   omit report, draft, source bodies and credentials. Observer failures must
   remain fail-closed. Add a small pure projection over existing trace events,
   keyed by run ID, for node status and separate telemetry fields. Do not infer
   success when a terminal publication event is absent.

Proposed comments at these boundaries:

```python
# ponytail: describe the real bounded graph; inert bindings never launch workers.
# ponytail: project existing trace metadata by run_id; no second run store.
# ponytail: graph_end is execution-only; publication needs the final adapter gate.
```

### Existing files: explicit approval required before editing

| File / smallest seam | Proposed change | Regression risk and required check |
| --- | --- | --- |
| `waku/ops/dashboard.py`, topology list and research metadata field | Import the dry topology/projection and append research without replacing triage/gather. Return the small projection under a research-only namespace. | Data generation must remain offline; original topology objects/settings/defaults unchanged; no observer/client created. |
| `waku/ops/static/js/views.js`, Graph tab | Add research-specific fail-closed/read-only text. Reuse `graphSVG` and render small per-run status rows with native `<details>` for metadata/usage. Scope the current generic fail-open copy to triage. | Escape labels, preserve unknown values and separate token/cost fields; no research launch control or mutation of gather's singleton `graphRun`. |

Proposed comments in the existing files:

```python
# ponytail: append read-only research data; existing workflow bindings stay untouched.
```

```javascript
// ponytail: reuse describe()/polling; research has no launch control here.
// ponytail: per-run projection, not gather's singleton; missing verdict stays unverified.
```

This is **two existing presentation/aggregation files**, not a Loop, Graph
engine, Memory, tracer, general release-gate or provider change. The current API
has no topology-registration hook, so zero existing-file edits cannot produce a
native live research panel without a separate UI, a hand-copied graph, or runtime
monkey-patching. Those alternatives are larger or less honest; do not use them.

Do not add a plugin framework, workflow registry redesign, durable run database,
WebSocket broker, pricing service, restart/resume system or export API for this
view. Metadata polling is sufficient for the first read-only monitor. The existing
chart animation is only trace replay; per-run status rows are authoritative. A
run-selected, timing-accurate node animation would additionally touch `graph.js`
and its event consumer, and is not the minimum proposed here. Cross-day replay,
concurrent-run controls and hard real-time timing remain explicit limits.

## Acceptance before UI changes enter the PR

- Dry topology build invokes no worker, client, settings loader or file write.
- `/research` and graph-stream requests without bindings remain BLOCKED and explain why.
- Existing triage/gather topology, routes and controls retain regression coverage.
- Sanitary fake-worker runs drive PASS, REVISE, INSUFFICIENT_EVIDENCE, exhausted
  budget, malformed evaluator, node error and observer error cases.
- Interleaved run IDs do not mix status rows or usage. Inactive branches are not
  reported as stalled workers. Unknown telemetry stays unknown; trace animation
  must not be labelled as a run-selected, wall-clock live monitor.
- `graph_end` without a terminal publication event is not displayed as PASS.
- Escape all metadata displayed as HTML; never place source/report bodies in events.
- No trace/report/run bundle is written by default; observer tracing is explicit.
- Exercise the UI in a clean, credential-free home on **127.0.0.1:7778**; leave live
  7777 untouched. The user must test the frontend; screenshots alone are not approval.

## PR recommendation

Keep the tested backend/command fix in the additive feature PR. Include this
annotated proposal as a reviewable document now. Apply the two existing-file UI
edits only after explicit scope approval, either as a separate commit in that PR
or a small follow-up PR. Do not present this proposal as an implemented UI.
