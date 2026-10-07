"""Wiring — builds one Waku from its parts. Gateways call `respond()`.

This file is the assembly diagram in code: config → db → tools → memory →
session → loop. If you want to understand the repo in one place, start here.
"""

from __future__ import annotations

import logging

from waku.config import Settings, load_settings
from waku.db import connect
from waku.loop import trim
from waku.loop.agent import LoopResult, Observer, run_loop
from waku.loop.models import get_client
from waku.memory import brain, reports
from waku.ops import receipt as receipts
from waku.ops.tracing import Tracer, compose, metered
from waku.runtime.session import Session
from waku.tools import build_registry
from waku.tools.waku_memory import get_via, remember_via, search_via, tool_name

# The Waku Memory tools that read memory. What they return this turn is
# already kept, so consolidation must not keep it again.
MEMORY_READS = ("memory.search", "memory.get", "memory.recall")

log = logging.getLogger(__name__)


class Waku:
    def __init__(self, settings: Settings | None = None, client=None, conn=None):
        # `client` and `conn` are injectable: evals swap in a scripted model,
        # the dashboard injects a cross-thread connection. Same seam either way.
        self.settings = settings or load_settings()
        self.settings.ensure_home()
        self.conn = conn or connect(self.settings.home)
        self.client = client or get_client(self.settings)

        # Memory first: the memory-management tools need it.
        from waku.memory import Memory

        self.memory = Memory(self.conn, self.settings, self.client)
        self.tools = build_registry(self.conn, self.settings, self.memory)
        self.mcp_bridge = getattr(self.tools, "mcp_bridge", None)
        # Spec 006: with Waku Memory connected, every fact consolidation keeps
        # is sent there too. Without it this is None and nothing is sent.
        self.memory.remember = remember_via(self.mcp_bridge)
        # Spec 009 A: a research turn searches Waku Memory before the model
        # starts. None without Waku Memory, and then nothing is searched.
        self.brain_search = search_via(self.mcp_bridge)
        self.brain_get = get_via(self.mcp_bridge)
        # A research report reaches Waku Memory one way, once a turn: from
        # the reply, through reports.save. The model's own remember tool
        # refuses a report body (2026-10-05: one turn saved two).
        remember_tool = self.tools.get(tool_name(self.mcp_bridge, "memory.remember") or "")
        if remember_tool is not None:
            remember_tool.fn = reports.guard_remember(remember_tool.fn)
        self.session = Session(self.settings, memory=self.memory)
        self.tracer = Tracer(self.settings)

    def close(self) -> None:
        """Release external resources (MCP subprocesses). Called when the
        dashboard rebuilds the agent after a settings change."""
        if self.mcp_bridge is not None:
            self.mcp_bridge.close()

    def respond(self, user_message: str, observer: Observer | None = None,
                source: str = "cli", stream: bool = False) -> LoopResult:
        """One full turn: assemble working memory → run the loop → persist.
        `source` tags which gateway the message arrived through (cli / voice /
        telegram / dashboard), so the unified chat can show its origin.
        `stream=True` streams the reply text token by token to the observer.
        Everything that happens is both shown (observer) and recorded (tracer)."""
        # capture the gate + graph decisions as they flow by, so we can persist
        # them with the turn (the reopened-thread telemetry the dashboard shows)
        import time
        captured: dict = {}
        # Spec 011: the events a receipt is built from, in the order they came
        turn_events: list[tuple[str, dict]] = []

        def _capture(kind, ev):
            if kind in ("llm", "tool", "consolidation", "report"):
                turn_events.append((kind, ev))
            if kind == "gate":
                captured["gate"] = {"decision": ev.get("decision"), "reason": ev.get("reason")}
            if kind == "route":
                captured["graph_route"] = {"target": ev.get("target"), "reason": ev.get("reason")}
            if kind == "triage":
                captured["triage_reason"] = ev.get("reason")
            if kind == "graph_end":
                captured["graph_path"] = ev.get("path")
        notify = compose(observer, self.tracer.event, _capture)
        t0 = time.perf_counter()

        with self.tracer.turn(user_message):
            if hasattr(self.client, "turn_id"):   # waku-platform: spec 011 B1
                self.client.turn_id = self.tracer.turn_id
            # The graph front door is optional and can NEVER make Waku worse:
            # flag off → this is exactly the old code path; flag on → the triage
            # graph decides quick vs full, and any failure anywhere falls open
            # to the plain loop below (same fail-open rule as the retrieval gate).
            result = None
            if self.settings.graph_workflows:
                try:
                    result = self._respond_via_graph(user_message, notify, stream)
                except Exception as exc:
                    notify("graph_end", {"workflow": "triage", "ms": 0, "steps": 0,
                                         "path": [], "error": repr(exc)})
                    result = None
            if result is None:
                result = self._run_full_turn(user_message, notify, stream)

            # Spec 007: a reply holding a research report sends the report to
            # Waku Memory and keeps only its first sentences in the chat. No
            # Waku Memory, or a failed send, leaves the reply whole.
            # One report a turn: when the model saved one itself, save()
            # sends nothing more and the card points at that memory.
            kept_report = reports.kept_body(result.reply, result.tool_calls)
            reply, report = reports.save(
                result.reply, self.memory.remember,
                lambda r: reports.is_company_research(
                    metered(self.client, "report", notify), self.settings.small_model, r),
                tool_calls=result.tool_calls)
            if report is not None:
                result.reply = reply
                notify("report", report)

            quick = captured.get("graph_route", {}).get("target") == "quick_reply"

            def _status(out: str) -> str:
                low = (out or "").lower()
                return "error" if ("failed" in low or "timed out" in low
                                   or low.startswith("error")) else "ok"
            meta = {
                "gate": captured.get("gate"),
                "graph": ({"workflow": "triage",
                           "route": "quick" if quick else "full",
                           "reason": captured.get("triage_reason", ""),
                           "path": captured.get("graph_path")}
                          if "graph_route" in captured else None),
                "iterations": result.iterations,
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "tools": [{"tool": c["tool"], "status": _status(c["output"])}
                          for c in result.read_first + result.tool_calls],
                # Spec 009 A: what the brain already knew, read before the turn
                "used": result.used,
                # which brain answered this turn — so a reopened thread (or a
                # thread you switched models mid-way) shows it per card. A quick
                # graph turn was answered by the small model; say so honestly.
                "model": self.settings.small_model if quick else self.settings.model,
                "provider": self.settings.provider,
                # the card a reopened thread draws in place of the report
                "report": report,
            }
            row_id = self.session.add_exchange(user_message, result.reply,
                                               tool_calls=result.tool_calls,
                                               source=source, meta=meta)
            if self.memory is not None:
                # Spec 009 B: a turn that saved a report keeps at most two
                # facts, about the person; the findings stay in the report.
                # A turn that answered from memory does not keep that memory
                # again (2026-10-05: a recall turn re-saved five findings).
                self.memory.maybe_consolidate(
                    notify=notify, report=kept_report if report is not None else "",
                    recalled=self._recalled(result))
                self.memory.export_markdown()   # keep MEMORY.md in sync

            # Spec 011: the receipt, once consolidation has kept what it keeps.
            # It goes on the result (the `done` payload), into the stored
            # row's meta, and into the trace as one `receipt` event. The reply
            # is already stored, so a receipt that fails is logged and skipped.
            try:
                self._receipt(result, turn_events, meta, row_id)
            except Exception:
                log.exception("the turn's receipt could not be built")

        self.tracer.end_turn(result.reply, result.iterations)
        return result

    def _recalled(self, result: LoopResult) -> str:
        """Everything this turn read from memory or saved to it, as text: what
        was read before the loop, what the model's own Waku Memory searches,
        gets and recalls returned, and what save_note saved."""
        reads = {tool_name(self.mcp_bridge, t) for t in MEMORY_READS} - {None}
        outputs = [c["output"] for c in result.tool_calls
                   if c.get("tool") in reads and isinstance(c.get("output"), str)]
        # Spec 018: a fact save_note already saved is memory this turn handled, so
        # consolidation must not extract the same sentence from the chat row again.
        saved = [f"{c['args'].get('subject', '')}: {c['args'].get('content', '')}"
                 for c in result.tool_calls
                 if c.get("tool") == "save_note" and isinstance(c.get("args"), dict)]
        return "\n".join(p for p in (result.recalled, *outputs, *saved) if p)

    def _receipt(self, result: LoopResult, events: list, meta: dict, row_id: int | None) -> None:
        charges = getattr(self.client, "charges", None)   # waku-platform only
        turn_id = self.tracer.turn_id
        result.receipt = receipts.build(
            events, turn_id=turn_id, model=meta["model"] or "",
            provider=self.settings.provider, default_model=self.settings.model or "",
            used=len(result.used),
            charges=charges(turn_id) if callable(charges) else None)
        meta.update(turn_id=turn_id, receipt=result.receipt)
        if row_id is not None:
            self.memory.update_meta(row_id, meta)
        self.tracer.event("receipt", result.receipt)

    def _run_full_turn(self, user_message: str, notify, stream: bool) -> LoopResult:
        """The classic turn: assemble working memory, run THE loop. Extracted
        verbatim so the graph's full_agent node calls the SAME code as the
        flag-off default — loop-as-a-node can never drift from loop-as-default."""
        system = self.session.build_system(user_message, notify=notify)
        # Working memory is a bounded window: only the last N turns (2 rows
        # each) enter the prompt, so context/cost/latency stay flat no matter
        # how long the conversation runs. Older turns live in state.db and
        # come back via the retrieval gate + episodic memory when relevant.
        window = self.settings.history_turns * 2
        messages = self.session.history[-window:] + [{"role": "user", "content": user_message}]

        # Spec 009 A: research starts from what the company brain already
        # knows. The searches run here, before the model's first call, and
        # are shown as tool calls; a failed search is skipped, never fatal.
        known = brain.ReadFirst()
        if self.brain_search is not None and brain.is_research(
                self.memory.skills.match(user_message)):
            known = brain.read_first(user_message, self.brain_search, self.brain_get)
            for call in known.calls:
                notify("tool", call)
            system += known.context

        result = run_loop(
            client=self.client,
            model=self.settings.model,
            system=system,
            messages=messages,
            tools=self.tools,
            max_iterations=self.settings.max_iterations,
            max_tokens=self.settings.max_tokens,
            observer=notify,
            stream=stream,
            # before each later call, a whole earlier report the model read
            # is cut to its digest (spec 009 A), and any other long result it
            # read is cut to its opening (waku/loop/trim.py)
            trim=trim.chain(reports.shrink_read, trim.shrink_seen),
        )
        result.read_first, result.used = known.calls, known.used
        result.recalled = "\n".join(p for p in (self.session.retrieved, known.context) if p)
        return result

    def _respond_via_graph(self, user_message: str, notify, stream: bool) -> LoopResult | None:
        """One turn through the triage graph workflow. Returns None whenever
        the graph didn't produce an answer — respond() then falls open to the
        plain loop, so this path can only ever ADD speed, never lose a reply."""
        from waku.graph import run_graph
        from waku.graph.workflows.triage import (
            QUICK_REPLY_PROMPT,
            build_triage_graph,
            classify_message,
            todays_events,
        )

        def quick_reply(state: dict) -> str:
            prompt = QUICK_REPLY_PROMPT.format(calendar=state.get("calendar", ""),
                                               message=state["message"])
            response = metered(self.client, "quick", notify).messages.create(
                model=self.settings.small_model, max_tokens=600,
                messages=[{"role": "user", "content": prompt}])
            return "".join(b.text for b in response.content if b.type == "text")

        graph = build_triage_graph(
            classify_fn=lambda m: classify_message(
                metered(self.client, "triage", notify), self.settings.small_model, m),
            calendar_fn=lambda: todays_events(self.settings.home),
            quick_fn=quick_reply,
            # the full path is the SAME method the flag-off default runs; the
            # engine's tagged notifier stamps its inner events with node=
            full_fn=lambda state: self._run_full_turn(
                state["message"], state.get("_notify", notify), stream),
        )
        state = run_graph(graph, {"message": user_message}, observer=notify)
        if isinstance(state.get("result"), LoopResult):
            return state["result"]
        if state.get("reply"):
            return LoopResult(reply=state["reply"], tool_calls=[], iterations=1)
        return None  # graph produced nothing → caller falls open to the loop
