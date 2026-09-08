"""Opt-in research contracts and runner. No clients, credentials or exports are created."""
from __future__ import annotations

import hashlib
import math
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import TypedDict, cast
from urllib.parse import urlsplit


class ResearchBlocked(ValueError):
    """A contract failed; there is no fallback to an unsourced loop answer."""


class Evidence(TypedDict):
    evidence_id: str
    claim: str
    source_url: str
    source_title: str
    source_type: str
    published_at: str | None
    accessed_at: str
    quote: str
    supports: bool
    confidence: float
    limitations: list[str]
    independence_group: str
    retrieved_by_task: str
    content_digest: str


class Draft(TypedDict):
    decision: str
    decisive_evidence: list[str]
    caveats: list[str]
    next_action: str
    core_sources: list[str]


class Evaluation(TypedDict):
    status: str
    evaluator_session_id: str
    verdict: str
    score: int
    threshold: int
    critical_failures: list[str]
    corrective_actions: list[str]
    route: str


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ResearchBlocked(reason)


def text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def strings(value) -> bool:
    return isinstance(value, (list, tuple)) and all(text(v) for v in value)


def web_url(value) -> bool:
    if not text(value) or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme in ("https", "http") and bool(parsed.hostname)
                and parsed.username is None and (parsed.port is None or parsed.port > 0))
    except ValueError:
        return False


def source_identity(url: str) -> tuple:
    """Page identity only; retain the original URL in every evidence record.

    Fragments, host case and default ports do not make a new page. Path/query
    semantics and redirect resolution belong to the trusted fetch adapter.
    """
    require(web_url(url), "invalid source URL")
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname, port, parsed.path or "/", parsed.query


def timestamp(value) -> bool:
    if not text(value):
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


@dataclass(frozen=True)
class Campaign:
    campaign_id: str
    run_id: str
    decision: str
    included: tuple[str, ...]
    excluded: tuple[str, ...]
    required_sources: tuple[str, ...]
    minimum_unique_sources: int = 8
    minimum_primary_sources: int = 5
    evaluator_threshold: int = 85
    counterevidence_required: bool = True
    max_revision_rounds: int = 1
    max_evidence_rounds: int = 2


@dataclass(frozen=True)
class Worker:
    fn: Callable
    session_id: str
    client: object
    provider: str
    model: str


TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                "reasoning_tokens")
COST_FIELDS = ("actual_cost_usd", "estimated_cost_usd")


@dataclass(frozen=True)
class Usage:
    """A disjoint usage increment, never a session summary added to its own details.

    Unknown cost/tokens stay None. Reasoning/cache may overlap provider token
    categories, so this adapter never manufactures a combined token total.
    """
    usage_id: str
    session_id: str
    parent_session_id: str | None
    source: str
    provider: str
    model: str
    api_calls: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    actual_cost_usd: float | None = None
    estimated_cost_usd: float | None = None
    billing_mode: str = "unknown"
    attribution_status: str = "unresolved"
    status: str = "completed"


@dataclass(frozen=True)
class Attempt:
    task_id: str
    session_id: str
    emit: Callable = field(repr=False)
    _sessions: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self._sessions[self.session_id] = (None, "worker")

    def _require(self, condition: bool, reason: str) -> None:
        if not condition:
            self.emit("research_telemetry_error", {"reason": reason})
            raise ResearchBlocked(reason)

    def start_descendant(self, session_id: str, *, parent_session_id: str, source: str) -> None:
        """Declare before dispatch so a lost child cannot disappear from the audit."""
        self._require(text(session_id) and session_id not in self._sessions
                      and text(parent_session_id) and parent_session_id in self._sessions
                      and source in ("tool", "subagent"), "invalid descendant telemetry linkage")
        self._sessions[session_id] = (parent_session_id, source)
        self.emit("research_descendant", {"session_id": session_id,
                  "parent_session_id": parent_session_id, "source": source})

    def record_usage(self, usage: Usage) -> None:
        self._require(isinstance(usage, Usage), "typed usage telemetry required")
        self._require(text(usage.session_id) and self._sessions.get(usage.session_id)
                      == (usage.parent_session_id, usage.source), "unattributed usage telemetry")
        self._require(all(text(v) for v in (usage.usage_id, usage.provider, usage.model)),
                      "missing usage identity")
        self._require(type(usage.api_calls) is int and usage.api_calls >= 0, "invalid API call count")
        for key in TOKEN_FIELDS:
            value = getattr(usage, key)
            self._require(value is None or (type(value) is int and value >= 0),
                          "invalid token telemetry")
        for key in COST_FIELDS:
            value = getattr(usage, key)
            self._require(value is None or (type(value) in (int, float) and math.isfinite(value)
                                           and value >= 0), "invalid cost telemetry")
        self._require(usage.billing_mode in ("metered", "subscription_included", "unknown")
                      and usage.attribution_status in ("exact", "bounded_fallback", "unresolved")
                      and usage.status in ("completed", "failed", "timed_out", "reclaimed"),
                      "invalid usage telemetry status")
        self.emit("research_usage", asdict(usage))


class ResearchAudit:
    """Append-only, run-local attempts; an explicit observer can persist the events."""

    def __init__(self, campaign: Campaign):
        self.campaign = campaign
        self.attempts: dict[str, dict] = {}
        self.usage: dict[tuple[str, str], dict] = {}
        self.descendants: list[dict] = []
        self.errors: list[dict] = []
        self.started = time.perf_counter()
        self.lock = threading.RLock()

    def invoke(self, task, fn, state, worker: Worker | None):
        session = worker.session_id if worker else f"local:{self.campaign.run_id}:{task}"
        identity = {"campaign_id": self.campaign.campaign_id, "run_id": self.campaign.run_id,
                    "task_id": task, "attempt_id": f"{self.campaign.run_id}:{task}:1", "attempt": 1}

        def emit(kind, event):
            record = {**event, **identity}
            with self.lock:
                if kind == "research_attempt_start":
                    require(task not in self.attempts, "duplicate attempt telemetry")
                    self.attempts[task] = {**record, "descendant_session_ids": []}
                elif kind == "research_attempt_end":
                    self.attempts[task].update(record)
                elif kind == "research_descendant":
                    self.descendants.append(record)
                    self.attempts[task]["descendant_session_ids"].append(event["session_id"])
                elif kind == "research_usage":
                    key = (identity["attempt_id"], event["usage_id"])
                    if key in self.usage:
                        emit("research_telemetry_error", {"reason": "duplicate usage telemetry"})
                        raise ResearchBlocked("duplicate usage telemetry")
                    self.usage[key] = record
                elif kind == "research_telemetry_error":
                    self.errors.append(record)
            state["_notify"](kind, deepcopy(record))

        state["_attempt"] = Attempt(task, session, emit)
        t0, status = time.perf_counter(), "failed"
        try:
            emit("research_attempt_start", {"status": "running",
                 "worker_session_ids": [session] if worker else [],
                 "provider": worker.provider if worker else "local",
                 "model": worker.model if worker else "code"})
            out = fn(state)
            status = "completed"
            return out
        except TimeoutError:
            status = "timed_out"
            raise
        finally:
            emit("research_attempt_end", {"status": status,
                 "worker_seconds": time.perf_counter() - t0})

    def validate(self, *, active_task: str | None = None) -> None:
        with self.lock:
            self._validate_attribution()
            for task, attempt in self.attempts.items():
                if task == active_task:
                    continue
                require(attempt["status"] == "completed", "incomplete attempt telemetry")

    def _validate_attribution(self) -> None:
        # Failed attempts still contribute known usage; completion gates publication separately.
        require(not self.errors, "invalid telemetry recorded")
        root_sessions = {s for a in self.attempts.values() for s in a["worker_session_ids"]}
        child_sessions = [d["session_id"] for d in self.descendants]
        require(not root_sessions.intersection(child_sessions)
                and len(set(child_sessions)) == len(child_sessions),
                "ambiguous descendant telemetry identity")
        for attempt in self.attempts.values():
            usage = [u for u in self.usage.values() if u["attempt_id"] == attempt["attempt_id"]]
            expected = set(attempt["worker_session_ids"] + attempt["descendant_session_ids"])
            require(expected == {u["session_id"] for u in usage}
                    and all(u["attribution_status"] != "unresolved" for u in usage),
                    "unattributed attempt or descendant telemetry")

    def snapshot(self) -> dict:
        with self.lock:
            usage = list(self.usage.values())
            complete = True
            try:
                self._validate_attribution()
            except ResearchBlocked:
                complete = False
            totals, observed = {}, {}
            for key in ("api_calls", *TOKEN_FIELDS, *COST_FIELDS):
                values = [u[key] for u in usage]
                known = [v for v in values if v is not None]
                observed[key] = sum(known) if known else None
                totals[key] = (observed[key] if complete and len(known) == len(values) else None)
            return deepcopy({"attempts": list(self.attempts.values()), "usage": usage,
                             "descendants": self.descendants, "totals": totals,
                             "observed_totals": observed, "attribution_complete": complete,
                             "errors": self.errors,
                             "wall_seconds": time.perf_counter() - self.started,
                             "worker_seconds": sum(a.get("worker_seconds", 0)
                                                   for a in self.attempts.values())})


def validate_campaign(campaign: Campaign) -> None:
    require(isinstance(campaign, Campaign), "invalid campaign")
    require(all(text(v) for v in (campaign.campaign_id, campaign.run_id, campaign.decision)),
            "campaign identity and decision required")
    require(all(isinstance(v, tuple) and strings(v)
                for v in (campaign.included, campaign.excluded, campaign.required_sources)),
            "scope and required sources must be immutable string tuples")
    require(bool(campaign.included) and all(web_url(u) for u in campaign.required_sources),
            "invalid scope or required source URL")
    for value, low, high in (
        (campaign.minimum_unique_sources, 1, 1000),
        (campaign.minimum_primary_sources, 0, campaign.minimum_unique_sources),
        (campaign.evaluator_threshold, 1, 100),
        (campaign.max_revision_rounds, 0, 1), (campaign.max_evidence_rounds, 1, 2),
    ):
        require(type(value) is int and low <= value <= high, "invalid campaign bound")
    require(type(campaign.counterevidence_required) is bool, "invalid counterevidence policy")


def validate_workers(workers: dict[str, Worker]) -> None:
    roles = {"plan", "primary", "counterevidence", "benchmark", "synthesize",
             "evaluate", "remediate"}
    require(set(workers) == roles, "all research workers, including evaluator, are required")
    require(all(isinstance(w, Worker) and callable(w.fn) and text(w.session_id)
                and text(w.provider) and text(w.model) and w.client is not None
                for w in workers.values()), "invalid worker configuration")
    require(len({w.session_id for w in workers.values()}) == len(roles),
            "research roles need independent sessions")
    evaluator = workers["evaluate"]
    require(all(w.client is not evaluator.client and w.fn is not evaluator.fn
                for role, w in workers.items() if role != "evaluate"),
            "evaluator needs an independent client and callable")


def validate_evaluation(raw, campaign: Campaign, session_id: str) -> Evaluation:
    require(isinstance(raw, dict) and Evaluation.__required_keys__ <= raw.keys(),
            "malformed evaluator metadata")
    require(raw["status"] == "completed" and raw["evaluator_session_id"] == session_id,
            "evaluator skipped or session missing")
    require(type(raw["score"]) is int and 0 <= raw["score"] <= 100, "invalid evaluator score")
    require(type(raw["threshold"]) is int and raw["threshold"] == campaign.evaluator_threshold,
            "evaluator cannot change acceptance threshold")
    require(strings(raw["critical_failures"]) and strings(raw["corrective_actions"]),
            "malformed evaluator findings")
    routes = {"PASS": "FINALIZE", "REVISE": "REVISION",
              "INSUFFICIENT_EVIDENCE": "EVIDENCE_ROUND"}
    require(isinstance(raw["verdict"], str) and raw["verdict"] in routes,
            "unknown evaluator verdict")
    require(raw["route"] == routes[raw["verdict"]], "unknown or inconsistent evaluator route")
    if raw["verdict"] == "PASS":
        require(raw["score"] >= campaign.evaluator_threshold and not raw["critical_failures"],
                "PASS below threshold or with critical failures")
    else:
        require(bool(raw["corrective_actions"]), "scoped corrective actions required")
    return cast(Evaluation, {k: raw[k] for k in Evaluation.__annotations__})


def verdict_route(raw, campaign: Campaign, session_id: str, revisions: int, rounds: int,
                  *, errors: bool = False) -> str:
    """Total code router: metadata is validated, prose and suggested routes never execute."""
    try:
        evaluation = validate_evaluation(raw, campaign, session_id)
    except (ValueError, TypeError, KeyError):
        return "BLOCK"
    if errors:
        return "BLOCK"
    if evaluation["verdict"] == "PASS":
        return "FINALIZE"
    if evaluation["verdict"] == "REVISE" and revisions < campaign.max_revision_rounds:
        return "REVISION"
    if evaluation["verdict"] == "INSUFFICIENT_EVIDENCE" and rounds < campaign.max_evidence_rounds:
        return "EVIDENCE_ROUND"
    return "BLOCK"


def normalize(batches: dict[str, dict], campaign: Campaign) -> dict:
    """Only materialized pages count. Workers are trusted fetch adapters, not prose."""
    unique: dict[str, Evidence] = {}
    counter_found = False
    notes = []
    for task, batch in batches.items():
        require(isinstance(batch, dict) and isinstance(batch.get("evidence"), list),
                "missing evidence batch")
        note = batch.get("counterevidence_note")
        if task.endswith("counterevidence") and text(note):
            notes.append(note)
        for raw in batch["evidence"]:
            require(isinstance(raw, dict) and Evidence.__required_keys__ <= raw.keys(),
                    "missing evidence fields")
            for key in ("evidence_id", "claim", "source_title", "quote", "independence_group"):
                require(text(raw[key]), f"invalid evidence {key}")
            require(web_url(raw["source_url"]), "invalid source URL")
            require(raw["source_type"] in ("primary", "secondary", "commentary"),
                    "invalid source type")
            require(timestamp(raw["accessed_at"]), "invalid access timestamp")
            require(raw["published_at"] is None or timestamp(raw["published_at"]),
                    "invalid publication timestamp")
            require(type(raw["supports"]) is bool, "invalid support polarity")
            confidence = raw["confidence"]
            require(type(confidence) in (int, float) and math.isfinite(confidence)
                    and 0 <= confidence <= 1, "invalid confidence")
            require(strings(raw["limitations"]), "invalid limitations")
            require(raw["retrieved_by_task"] == task, "unattributed evidence")
            content = raw.get("source_content")
            require(raw.get("retrieval_kind") == "page" and text(content),
                    "materialized page required; snippets/model knowledge are not evidence")
            require(raw["quote"] in content, "quote not in source content")
            digest = "sha256:" + hashlib.sha256(cast(str, content).encode("utf-8")).hexdigest()
            require(raw["content_digest"] == digest, "source digest mismatch")
            record = cast(Evidence, {k: raw[k] for k in Evidence.__annotations__})
            old = unique.get(record["evidence_id"])
            require(old is None or (source_identity(old["source_url"])
                    == source_identity(record["source_url"])
                    and all(old[k] == record[k] for k in record
                            if k not in ("retrieved_by_task", "accessed_at", "source_url"))),
                    "conflicting evidence ID")
            unique.setdefault(record["evidence_id"], record)
            if task.endswith("counterevidence") and not raw["supports"]:
                counter_found = True
    evidence = list(unique.values())
    sources = {source_identity(e["source_url"]) for e in evidence}
    primary = {source_identity(e["source_url"]) for e in evidence if e["source_type"] == "primary"}
    require({source_identity(u) for u in campaign.required_sources} <= sources,
            "missing required source")
    require(len(sources) >= campaign.minimum_unique_sources, "insufficient unique sources")
    require(len(primary) >= campaign.minimum_primary_sources, "insufficient primary sources")
    require(not campaign.counterevidence_required or counter_found or bool(notes),
            "counterevidence search missing")
    return {"evidence": evidence, "unique_sources": len(sources), "primary_sources": len(primary),
            "counterevidence_notes": notes}


def validate_draft(raw, evidence: list[Evidence]) -> Draft:
    require(isinstance(raw, dict) and Draft.__required_keys__ <= raw.keys(),
            "missing substantive draft")
    require(text(raw["decision"]) and text(raw["next_action"]), "empty decision or next action")
    for key in ("decisive_evidence", "caveats", "core_sources"):
        require(strings(raw[key]) and bool(raw[key]), f"empty or invalid draft {key}")
    sources = {e["evidence_id"]: e["source_url"] for e in evidence}
    require(set(raw["decisive_evidence"]) <= sources.keys(), "unknown decisive evidence")
    require(set(raw["core_sources"]) <= set(sources.values()), "unknown core sources")
    require({sources[e] for e in raw["decisive_evidence"]} <= set(raw["core_sources"]),
            "decisive evidence missing from core sources")
    return cast(Draft, {k: raw[k] for k in Draft.__annotations__})


def render_report(draft: Draft, evaluation: Evaluation) -> str:
    return (f"# Research decision\n\n{evaluation['verdict']} "
            f"{evaluation['score']}/{evaluation['threshold']}\n\n"
            f"{draft['decision']}\n\nDecisive evidence: "
            + ", ".join(draft["decisive_evidence"])
            + "\n\nCaveats: " + "; ".join(draft["caveats"])
            + f"\n\nNext action: {draft['next_action']}\n\nCore sources:\n"
            + "\n".join(draft["core_sources"]))


def run_research(campaign: Campaign | None = None, workers: dict[str, Worker] | None = None,
                 *, observer=None, max_steps: int = 100, message: str = "") -> dict:
    """Run injected workers only; accepted reports are returned in memory."""
    # ponytail: reuse slash/SSE discovery; no live binder until start approval exists.
    if campaign is None or workers is None or message:
        errors = {"configuration": "explicit Campaign and worker bindings required"}
        return {"publication_allowed": False, "status": "BLOCKED", "report": None,
                "digest": "Research not started. Supply an explicit Campaign and worker bindings "
                          "through the Python API; live start requires separate approval.",
                "errors": errors, "state": {"errors": errors}, "telemetry": {}}

    from waku.graph.engine import run_graph
    from waku.graph.workflows.research import build_research_graph

    audit = ResearchAudit(campaign)
    state = {"errors": {}}

    def observe(kind, event):
        if observer is not None:
            try:
                observer(kind, {**deepcopy(event), "campaign_id": campaign.campaign_id,
                                "run_id": campaign.run_id})
            except Exception as exc:
                # An adapter may catch an emit failure; it must not reopen publication.
                reason = f"research observer failed ({type(exc).__name__})"
                state["errors"]["observer"] = reason
                raise ResearchBlocked(reason) from None

    try:
        graph = build_research_graph(campaign, workers, audit=audit)
        run_graph(graph, state, observer=observe, max_steps=max_steps)
        audit.validate()
    except ResearchBlocked as exc:
        state["errors"]["contract"] = str(exc)
    except Exception as exc:
        state["errors"]["engine"] = type(exc).__name__
    allowed = bool(state.get("report")) and not state["errors"]
    if not allowed:
        state.pop("report", None)
    return {"publication_allowed": allowed, "status": "PASS" if allowed else "BLOCKED",
            "report": state.get("report") if allowed else None,
            "errors": state["errors"], "state": state, "telemetry": audit.snapshot()}
