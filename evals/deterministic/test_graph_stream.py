"""DETERMINISTIC EVAL — /api/graph/stream, the socket behind the wave cards.

The cards in the Graph tab are only as honest as the events reaching them, and
three ways to break that are invisible until a live run:

  * a workflow name arriving from the browser gets imported and called
  * a value that json.dumps cannot encode kills the stream mid-frame, so the
    browser sees a half-finished run and no error
  * a node raises and the whole response dies instead of reporting

None of that needs a model, a network or a real workflow — a scripted graph and
a fake emit are enough. Offline, sub-second.
"""

from __future__ import annotations

import json

import pytest

from waku.graph import END, START, Graph, Node
from waku.ops import dashboard


def _emitter():
    out: list[tuple[str, dict]] = []
    return out, lambda kind, ev: out.append((kind, ev))


def test_an_unknown_workflow_is_refused_without_importing_anything():
    """The runner table is a dict for exactly this reason. 'run the workflow the
    client named' is one careless refactor from 'import whatever string
    arrives', so the refusal is pinned."""
    out, emit = _emitter()
    for name in ("", "nope", "waku.ops.gather", "os:system", "../../etc/passwd"):
        out.clear()
        dashboard.graph_stream({"workflow": name}, emit)
        assert len(out) == 1 and out[0][0] == "done"
        assert "unknown workflow" in out[0][1]["error"], out


def test_the_runner_table_maps_names_to_known_targets():
    assert dashboard.WORKFLOW_RUNNERS == {"gather": "waku.ops.gather:run_gather"}
    for target in dashboard.WORKFLOW_RUNNERS.values():
        module, _, fn = target.partition(":")
        assert module.startswith("waku.") and fn


@pytest.fixture
def scripted(monkeypatch):
    """Swap the gather runner for a tiny scripted graph — same engine, same
    events, no model and no network."""
    def fake_run(observer=None, **kw):
        g = Graph("gather")
        g.add_node(Node("a", lambda s: {"x": 1}, kind="tool"))
        g.add_node(Node("b", lambda s: {"y": 2}, kind="tool"))
        g.add_node(Node("c", lambda s: {"digest": "D"}, kind="llm"))
        for n in ("a", "b"):
            g.add_edge(START, n)
            g.add_edge(n, "c")
        g.add_edge("c", END)
        from waku.graph import run_graph

        return run_graph(g, {}, observer=observer)

    import waku.ops.gather as gather_mod

    monkeypatch.setattr(gather_mod, "run_gather", fake_run)
    return fake_run


def test_the_fan_out_is_visible_in_the_event_order(scripted):
    """Both parallel nodes must START before either ENDS. That ordering is the
    only thing the cards use to group a wave, so if it stops holding the UI
    silently draws two waves of one instead of one wave of two."""
    out, emit = _emitter()
    dashboard.graph_stream({"workflow": "gather"}, emit)
    kinds = [(k, ev.get("node")) for k, ev in out]
    starts = [i for i, (k, n) in enumerate(kinds) if k == "node_start" and n in ("a", "b")]
    first_end = next(i for i, (k, n) in enumerate(kinds) if k == "node_end")
    assert len(starts) == 2 and max(starts) < first_end, kinds


def test_every_event_survives_json_dumps(scripted):
    """The endpoint writes `data: {json}` per frame. One unencodable value —
    a Path, a LoopResult — and the socket dies mid-stream with a traceback the
    browser never sees, leaving the cards frozen and blameless."""
    out, emit = _emitter()
    dashboard.graph_stream({"workflow": "gather"}, emit)
    assert out
    for kind, ev in out:
        json.dumps({"kind": kind, **ev}, default=str)


def test_node_output_never_rides_along(scripted):
    """Cards need names and timings, not payloads. node_end carrying a digest
    would put the whole briefing into every frame."""
    for kind, ev in [(k, e) for k, e in _run(scripted) if k == "node_end"]:
        assert set(ev) <= {"workflow", "node", "ms", "keys", "error"}, ev


def _run(_):
    out, emit = _emitter()
    dashboard.graph_stream({"workflow": "gather"}, emit)
    return out


def test_the_run_ends_with_a_done_carrying_the_digest(scripted):
    out = _run(scripted)
    assert out[-1][0] == "done"
    assert out[-1][1]["digest"] == "D"
    assert out[-1][1]["workflow"] == "gather"


def test_a_raising_node_still_finishes_the_stream(monkeypatch):
    """A dead node must not strand the browser. graph_end and done both still
    arrive, so the cards settle instead of spinning forever."""
    def fake_run(observer=None, **kw):
        g = Graph("gather")

        def boom(_s):
            raise RuntimeError("scan died")

        g.add_node(Node("a", boom, kind="tool"))
        g.add_edge(START, "a")
        g.add_edge("a", END)
        from waku.graph import run_graph

        return run_graph(g, {}, observer=observer)

    import waku.ops.gather as gather_mod

    monkeypatch.setattr(gather_mod, "run_gather", fake_run)
    out = _run(None)
    assert any(k == "graph_end" for k, _ in out)
    assert out[-1][0] == "done"


def test_a_runner_that_explodes_reports_instead_of_500ing(monkeypatch):
    """GraphStateCollision is raised OUT of run_graph, unlike node errors. It
    must surface in the card rather than drop the socket."""
    def boom(**kw):
        raise RuntimeError("collision")

    import waku.ops.gather as gather_mod

    monkeypatch.setattr(gather_mod, "run_gather", boom)
    out = _run(None)
    assert out[-1][0] == "done" and "collision" in out[-1][1]["error"]
