"""DETERMINISTIC EVAL -- scripts/chat_speed.py, the measuring script of spec 039 A1.

The script sends a fixed set of questions to `/v1/chat` and records, for each
one, when the first event, the first word and `done` arrived, how many model
calls the turn made and their tokens, and what the turn was charged. These
tests run it against a fake server on loopback that replays a recorded
stream, so the parsing and the CSV are checked without a network or a key.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

spec = importlib.util.spec_from_file_location(
    "chat_speed", Path(__file__).resolve().parents[2] / "scripts" / "chat_speed.py")
chat_speed = importlib.util.module_from_spec(spec)
sys.modules["chat_speed"] = chat_speed
spec.loader.exec_module(chat_speed)

TOKEN = "eyJ-test-token-never-printed"

# One turn as the container streams it: the retrieval gate's decision and
# its side call, two loop calls with a tool between them, then `done` with the
# turn's receipt. The container writes each frame as {"kind": kind, **event},
# and a side call's event carries its own `kind` ("gate"), which wins the
# merge: a side call arrives as kind "gate" with a `usage`, and only the
# loop's own calls arrive as kind "llm".
FRAMES = [
    {"kind": "gate", "decision": "retrieve", "reason": "names a person"},
    {"kind": "gate", "model": "claude-haiku-4-5", "usage": {"in": 900, "out": 12}},
    {"kind": "llm", "iteration": 1, "stop_reason": "tool_use",
     "usage": {"in": 30000, "out": 80, "cache_read": 0, "cache_write": 12000}},
    {"kind": "tool", "tool": "waku_memory_memory_search", "call_id": "tu_1",
     "args": {}, "output": "{}"},
    {"kind": "text", "delta": "Emily "},
    {"kind": "text", "delta": "is your designer."},
    {"kind": "llm", "iteration": 2, "stop_reason": "end_turn",
     "usage": {"in": 18500, "out": 40, "cache_read": 12000, "cache_write": 0}},
    {"kind": "done", "reply": "Emily is your designer.",
     "receipt": {"turn_id": "t-abc", "model": {"usd": 0.0712, "estimate": False},
                 "credits": 2400}},
]


class _Handler(BaseHTTPRequestHandler):
    frames: ClassVar[list] = FRAMES
    delay = 0.02
    seen: ClassVar[list] = []

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802 - the stdlib's name
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                "body": body})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for frame in type(self).frames:
            time.sleep(type(self).delay)
            self.wfile.write(f"data: {json.dumps(frame)}\n\n".encode())
            self.wfile.flush()


@pytest.fixture
def server():
    _Handler.seen = []
    _Handler.frames = FRAMES
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_there_are_ten_questions_and_the_two_the_spec_names():
    assert len(chat_speed.QUESTIONS) == 10
    assert len(set(chat_speed.QUESTIONS)) == 10
    assert "What did I decide about pricing last week?" in chat_speed.QUESTIONS
    assert "Who is Emily?" in chat_speed.QUESTIONS


def test_one_question_is_timed_and_its_calls_counted(server):
    row = chat_speed.ask(server, TOKEN, "Who is Emily?")
    seen = _Handler.seen[0]
    assert seen["path"] == "/v1/chat"
    assert seen["auth"] == f"Bearer {TOKEN}"
    assert seen["body"] == {"message": "Who is Emily?"}
    # Each frame waits 20 ms, so the first event lands after one frame, the
    # first word after five and `done` after all eight.
    assert 0.015 <= row["first_event_s"] < row["first_text_s"] < row["done_s"]
    assert row["first_text_s"] >= 0.09
    assert row["done_s"] >= 0.15
    assert row["model_calls"] == 3
    assert row["loop_calls"] == 2
    assert row["input_tokens"] == 49400
    assert row["cache_read_tokens"] == 12000
    assert row["cache_write_tokens"] == 12000
    assert row["output_tokens"] == 132
    assert row["calls"] == "gate:900/0/0/12; loop:30000/0/12000/80; loop:18500/12000/0/40"
    assert row["turn_id"] == "t-abc"
    assert row["model_usd"] == 0.0712
    assert row["usd_is_estimate"] is False
    assert row["credits"] == 2400
    assert row["error"] == ""


def test_a_side_call_is_counted_under_its_own_kind(server):
    _Handler.frames = [
        {"kind": "consolidation", "model": "claude-haiku-4-5", "usage": {"in": 700, "out": 30}},
        {"kind": "final", "final_answer": True, "usage": {"in": 5000, "out": 60}},
        {"kind": "done", "reply": "hi"},
    ]
    row = chat_speed.ask(server, TOKEN, "hello")
    assert row["model_calls"] == 2
    assert row["loop_calls"] == 1
    assert row["calls"] == "consolidation:700/0/0/30; final:5000/0/0/60"


def test_a_stream_without_cache_fields_counts_zero_cache_tokens(server):
    """Today's container reports only `in` and `out`, so a baseline run must
    still work and show zero cache tokens rather than fail."""
    _Handler.frames = [
        {"kind": "llm", "iteration": 1, "usage": {"in": 40000, "out": 50}},
        {"kind": "done", "reply": "ok", "receipt": {"model": {"usd": 0.08, "estimate": True}}},
    ]
    row = chat_speed.ask(server, TOKEN, "hello")
    assert (row["input_tokens"], row["cache_read_tokens"], row["cache_write_tokens"]) == (40000, 0, 0)
    assert row["usd_is_estimate"] is True
    assert row["credits"] is None
    assert row["first_text_s"] is None


def test_a_refusal_is_recorded_as_the_error(server):
    _Handler.frames = [{"kind": "done", "error": "Your assistant is taking too long to start."}]
    row = chat_speed.ask(server, TOKEN, "hello")
    assert row["error"] == "Your assistant is taking too long to start."
    assert row["model_calls"] == 0


def test_an_http_error_is_recorded_and_the_run_goes_on(tmp_path):
    class Refuse(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = b'{"error": "Sign in again."}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Refuse)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        row = chat_speed.ask(f"http://127.0.0.1:{httpd.server_address[1]}", TOKEN, "hello")
    finally:
        httpd.shutdown()
    assert row["error"] == "HTTP 401: Sign in again."


def test_main_writes_one_csv_row_per_question_and_never_prints_the_token(server, tmp_path, capsys):
    out = tmp_path / "warm.csv"
    code = chat_speed.main(["--label", "warm", "--out", str(out), "--limit", "3"],
                           env={"WAKU_CHAT_TOKEN": TOKEN, "WAKU_CHAT_URL": server})
    assert code == 0
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [r["n"] for r in rows] == ["1", "2", "3"]
    assert {r["label"] for r in rows} == {"warm"}
    assert rows[0]["question"] == chat_speed.QUESTIONS[0]
    assert list(rows[0]) == list(chat_speed.CSV_FIELDS)
    printed = capsys.readouterr()
    assert TOKEN not in printed.out + printed.err
    assert TOKEN not in out.read_text(encoding="utf-8")


def test_main_refuses_without_the_token_in_the_environment(tmp_path, capsys):
    code = chat_speed.main(["--label", "warm", "--out", str(tmp_path / "x.csv")], env={})
    assert code == 2
    assert "WAKU_CHAT_TOKEN" in capsys.readouterr().err
    assert not (tmp_path / "x.csv").exists()


def test_the_token_is_not_a_command_line_option():
    with pytest.raises(SystemExit):
        chat_speed.parse_args(["--label", "warm", "--token", TOKEN])


def test_the_default_base_url_is_the_hosted_front_door():
    assert chat_speed.DEFAULT_URL == "https://agent.waku.one"


def test_the_local_parts_estimate_counts_each_part(tmp_path, monkeypatch):
    """The per-part split is built locally from the hosted registry and
    counted by characters / 4, and says it is an estimate."""
    monkeypatch.delenv("WAKU_PLATFORM_BASE_URL", raising=False)
    monkeypatch.delenv("WAKU_PLATFORM_TOKEN", raising=False)
    parts = chat_speed.local_parts("Who is Emily?", home=tmp_path)
    assert parts["estimate"] is True
    assert parts["tool_count"] > 10
    assert parts["tools"] > 1000
    assert parts["system"] > 300
    assert parts["message"] == len("Who is Emily?") // 4
    assert parts["history"] == 0
