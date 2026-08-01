"""DETERMINISTIC EVAL — the Compare arena's own history log.

The arena is a benchmark, not a conversation, so its runs must NOT land in the
agent's real state (chat_log / facts / calendar). They live in their own capped
JSONL, and the per-model scoreboard is a scan over it. These tests pin the store
contract: append + trim, isolation of the reply cap, and the aggregate math."""

from __future__ import annotations

from milli.ops import compare_history as ch


def _result(spec, model, latency, tin, tout, cost, error=None):
    return {"spec": spec, "provider": spec.split(":")[0], "model": model,
            "latency_ms": latency, "tokens_in": tin, "tokens_out": tout,
            "cost_usd": cost, "iterations": 1, "gate": {"decision": "skip"},
            "tools": [{"tool": "create_event"}], "error": error, "reply": "ok"}


def test_append_and_load_round_trip(tmp_path):
    ch.append_run(tmp_path, "hi", [_result("kimi:kimi-k3", "kimi-k3", 1000, 10, 5, 0.01)])
    runs = ch.load_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0]["message"] == "hi"
    r = runs[0]["results"][0]
    assert r["model"] == "kimi-k3" and r["gate"] == "skip" and r["tools"] == ["create_event"]


def test_it_writes_to_its_own_file_not_state_db(tmp_path):
    ch.append_run(tmp_path, "hi", [_result("a:b", "b", 1, 1, 1, 0.0)])
    assert (tmp_path / "compare" / "history.jsonl").exists()
    assert not (tmp_path / "state.db").exists()   # never touches the agent's DB


def test_clear_wipes_only_the_history(tmp_path):
    ch.append_run(tmp_path, "hi", [_result("a:b", "b", 1, 1, 1, 0.0)])
    (tmp_path / "state.db").write_text("real data")   # a sibling that must survive
    ch.clear(tmp_path)
    assert ch.load_runs(tmp_path) == []
    assert not (tmp_path / "compare" / "history.jsonl").exists()
    assert (tmp_path / "state.db").read_text() == "real data"   # untouched


def test_history_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(ch, "MAX_RUNS", 3)
    for i in range(5):
        ch.append_run(tmp_path, f"run {i}", [_result("a:b", "b", 1, 1, 1, 0.0)])
    runs = ch.load_runs(tmp_path)
    assert [r["message"] for r in runs] == ["run 2", "run 3", "run 4"]   # oldest rolled off


def test_reply_is_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(ch, "REPLY_CAP", 10)
    big = {**_result("a:b", "b", 1, 1, 1, 0.0), "reply": "x" * 500}
    ch.append_run(tmp_path, "hi", [big])
    assert len(ch.load_runs(tmp_path)[0]["results"][0]["reply"]) == 10


def test_aggregate_totals_over_successful_runs_only(tmp_path):
    runs = [
        {"message": "q1", "results": [_result("k:m", "m", 1000, 100, 100, 0.02),
                                      _result("g:n", "n", 2000, 200, 200, 0.04, error="boom")]},
        {"message": "q2", "results": [_result("k:m", "m", 3000, 300, 300, 0.06)]},
    ]
    agg = {a["spec"]: a for a in ch.aggregate(runs)}
    m = agg["k:m"]
    assert m["runs"] == 2 and m["ok"] == 2
    assert m["total_latency_ms"] == 4000 and m["total_cost_usd"] == 0.08
    # tokens split in/out and still total; helper sends tin=tout each run (100+300)
    assert m["total_tokens_in"] == 400 and m["total_tokens_out"] == 400
    assert m["total_tokens"] == 800 == m["total_tokens_in"] + m["total_tokens_out"]
    n = agg["g:n"]
    assert n["runs"] == 1 and n["ok"] == 0          # errored run counted, adds nothing to totals
    assert n["total_cost_usd"] == 0.0
