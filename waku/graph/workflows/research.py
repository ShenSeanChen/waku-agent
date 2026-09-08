"""Independent evidence waves around the unchanged Waku graph engine."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

from waku.graph.engine import END, START, Graph, Node
from waku.ops.research import (
    Campaign,
    ResearchAudit,
    ResearchBlocked,
    Worker,
    normalize,
    render_report,
    require,
    strings,
    text,
    validate_campaign,
    validate_draft,
    validate_evaluation,
    validate_workers,
    verdict_route,
)

STREAMS = ("primary", "counterevidence", "benchmark")


def build_research_graph(campaign: Campaign, workers: dict[str, Worker], *,
                         audit: ResearchAudit | None = None) -> Graph:
    """Unroll at most one revision and one extra evidence wave, in either order.

    Each path has distinct node names; there are no backward edges or retries.
    Building/describing the graph never invokes a worker.
    """
    validate_campaign(campaign)
    validate_workers(workers)
    graph = Graph("research")
    audit = audit or ResearchAudit(campaign)

    def call(role, payload, state):
        worker = workers[role]
        try:
            return deepcopy(worker.fn(deepcopy(payload), state["_attempt"]))
        except TimeoutError:
            raise TimeoutError("research worker timed out") from None
        except Exception as exc:
            raise ResearchBlocked(f"worker result/telemetry failed ({type(exc).__name__})") from None

    def plan(s):
        out = call("plan", {"campaign": asdict(campaign)}, s)
        require(isinstance(out, dict) and all(text(out.get(r)) for r in STREAMS),
                "planner must scope every evidence workstream")
        return {"plan": {r: out[r] for r in STREAMS}}

    def finalize(s):
        audit.validate(active_task="finalize")
        require(not s["errors"] and verdict_route(s.get("evaluation"), campaign,
                workers["evaluate"].session_id, 0, 1) == "FINALIZE", "publication gate closed")
        draft = validate_draft(s["draft"], s["normalized"]["evidence"])
        return {"report": render_report(draft, s["evaluation"])}

    graph.add_node(Node("plan", plan))
    graph.add_node(Node("finalize", finalize))
    def block(s) -> dict:
        raise ResearchBlocked("recovery budget exhausted")

    graph.add_node(Node("block", block))
    graph.add_edge(START, "plan")
    graph.add_edge("finalize", END)

    def synthesis(prefix, parent, batches, revisions, rounds):
        synth, evaluate = prefix + "synthesize", prefix + "evaluate"

        def synthesize(s):
            draft = validate_draft(call("synthesize", {
                "campaign": asdict(campaign), **s["normalized"],
                "corrections": s.get("corrections", []),
            }, s), s["normalized"]["evidence"])
            return {"draft": draft, prefix + "draft": draft}

        def evaluate_draft(s):
            evaluation = validate_evaluation(call("evaluate", {
                "campaign": asdict(campaign), "draft": s["draft"], **s["normalized"],
            }, s), campaign, workers["evaluate"].session_id)
            return {"evaluation": evaluation, prefix + "evaluation": evaluation}

        graph.add_node(Node(synth, synthesize, kind="llm"))
        graph.add_node(Node(evaluate, evaluate_draft, kind="llm"))
        graph.add_edge(parent, synth)
        graph.add_edge(synth, evaluate)
        targets = {"FINALIZE": "finalize", "BLOCK": "block"}
        if revisions < campaign.max_revision_rounds:
            revision = prefix + "revision_"
            remediate = revision + "remediate"

            def correct(s):
                out = call("remediate", {
                    "campaign": asdict(campaign), "draft": s["draft"], **s["normalized"],
                    "corrective_actions": s["evaluation"]["corrective_actions"],
                }, s)
                require(isinstance(out, dict) and strings(out.get("corrections"))
                        and bool(out["corrections"]), "empty remediation")
                return {"corrections": out["corrections"]}

            graph.add_node(Node(remediate, correct, kind="llm"))
            synthesis(revision, remediate, batches, revisions + 1, rounds)
            targets["REVISION"] = remediate
        if rounds < campaign.max_evidence_rounds:
            extra = prefix + "evidence2_"
            scope = extra + "scope"
            graph.add_node(Node(scope, lambda s: {
                extra + "actions": s["evaluation"]["corrective_actions"]}))
            evidence_wave(extra, scope, batches, revisions, rounds + 1)
            targets["EVIDENCE_ROUND"] = scope
        graph.add_router(evaluate, lambda s: verdict_route(
            s.get("evaluation"), campaign, workers["evaluate"].session_id,
            revisions, rounds, errors=bool(s["errors"])), targets)

    def evidence_wave(prefix, parent, previous, revisions, rounds):
        batches = previous + [prefix + r for r in STREAMS]
        for role in STREAMS:
            task = prefix + role
            graph.add_node(Node(task, lambda s, r=role, t=task: {t: call(r, {
                "campaign": asdict(campaign), "scope": s["plan"][r],
                "evidence_round": rounds, "corrective_actions": s.get(prefix + "actions", []),
            }, s)}, kind="tool"))
            graph.add_edge(parent, task)
        normalizer = prefix + "normalize"
        graph.add_node(Node(normalizer, lambda s: {
            "normalized": normalize({b: s[b] for b in batches}, campaign)}))
        for role in STREAMS:
            graph.add_edge(prefix + role, normalizer)
        synthesis(prefix, normalizer, batches, revisions, rounds)

    evidence_wave("", "plan", [], 0, 1)
    for name, node in graph.nodes.items():
        worker = next((w for role, w in workers.items() if name.endswith(role)), None)
        original = node.fn
        node.fn = lambda s, n=name, fn=original, w=worker: audit.invoke(n, fn, s, w)
    return graph
