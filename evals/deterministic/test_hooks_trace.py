"""Spec 021 C4: each plugin hook call is a `hook` span in its turn, Stop
included, although Stop's line is written after the turn ended."""

from __future__ import annotations

from evals.deterministic.test_hooks import FakeBridge, _stopped, _waku, brief
from waku.ops import observability


def test_every_hook_call_is_a_hook_step_in_its_turn(tmp_path):
    bridge = FakeBridge({"UserPromptSubmit": brief("UserPromptSubmit", "Ian deploys on Fridays.")})
    waku, _ = _waku(tmp_path, bridge, replies=("You ship on Fridays.", "Yes."))
    waku.respond("can I ship today?", source="dashboard")
    _stopped(waku)
    waku.respond("and tomorrow?", source="dashboard")
    _stopped(waku)

    events, _, _ = observability.read_events(tmp_path)
    turns = observability.group_turns(events)
    assert len(turns) == 2
    first = observability.build_turn(turns[0])
    hooks = [s for s in first["steps"] if s["kind"] == "hook"]
    assert [h["event"] for h in hooks] == ["SessionStart", "UserPromptSubmit", "Stop"]
    assert all(h["span"] == "hook" and h["ok"] and h["plugin"] == "waku" for h in hooks)
    assert hooks[1]["context_chars"] == len("Ian deploys on Fridays.")
    second = observability.build_turn(turns[1])
    assert [s["event"] for s in second["steps"] if s["kind"] == "hook"] == ["UserPromptSubmit", "Stop"]


def test_a_stop_line_that_lands_inside_the_next_turn_joins_its_own():
    events = [
        {"type": "turn_start", "turn_id": "t_a", "ts": "2026-11-12T10:00:00+00:00", "user_message": "one"},
        {"type": "turn_end", "turn_id": "t_a", "ts": "2026-11-12T10:00:02+00:00", "reply": "1"},
        {"type": "turn_start", "turn_id": "t_b", "ts": "2026-11-12T10:00:03+00:00", "user_message": "two"},
        {"type": "hook", "turn_id": "t_a", "event": "Stop", "ok": False, "error": "timed out after 10s",
         "ts": "2026-11-12T10:00:04+00:00"},
        {"type": "turn_end", "turn_id": "t_b", "ts": "2026-11-12T10:00:05+00:00", "reply": "2"},
    ]
    first, second = observability.group_turns(events)
    assert [e["event"] for e in first["events"]] == ["Stop"] and second["events"] == []
    step = observability.build_turn(first)["steps"][0]
    assert step["kind"] == "hook" and step["ok"] is False and step["error"] == "timed out after 10s"
