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


def test_trace_lines_written_from_two_threads_stay_whole(tmp_path, monkeypatch):
    """Review fix 13: the Stop thread and the next turn write at once."""
    import builtins
    import json
    import threading
    import time

    from waku.config import Settings
    from waku.ops.tracing import Tracer

    tracer = Tracer(Settings(home=tmp_path))
    tracer.path.parent.mkdir(parents=True, exist_ok=True)
    real_open = builtins.open

    class SlowFile:
        """Writes in two halves with a pause, the way a long line can."""

        def __init__(self, f):
            self.f = f

        def write(self, text):
            half = len(text) // 2
            self.f.write(text[:half])
            self.f.flush()
            time.sleep(0.01)
            self.f.write(text[half:])

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()

    monkeypatch.setattr(type(tracer.path), "open",
                        lambda self, *a, **k: SlowFile(real_open(self, *a, **k)))
    threads = [threading.Thread(target=lambda n=n: [tracer.event("hook", {"n": n, "pad": "x" * 200})
                                                    for _ in range(5)]) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    monkeypatch.undo()
    lines = tracer.path.read_text().splitlines()
    assert len(lines) == 20 and all(json.loads(line)["type"] == "hook" for line in lines)
