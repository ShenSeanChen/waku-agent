"""DETERMINISTIC EVAL -- chat history never shows or re-sends a raw tool output.

Seen on dev.waku.one, 2026-10-03: a reopened conversation drew a block like

    [tools used: treg_catalog_search({'query': ...}) -> {"query": ..., "results": [ ...

thousands of lines tall (one element measured 138,371 px). Two halves:

  1. Storage. Session.add_exchange appended "[tools used: tool(args) -> FULL
     output]" to the assistant record. That record is the history the model is
     sent on every later turn and the chat_log row, so one catalog search was
     re-sent to the model on every turn after it. The note now keeps the tool's
     name, short args and at most ~300 characters of output (tool_note.py), and
     a row written before that is clipped wherever old history is read back.
  2. Rendering. render.js stripped the note only in the plain card; a row with
     meta (every dashboard and embedded-chat turn) went histItem -> chatTurnCard
     and drew the whole note. histItem now strips it for every stored row, and
     the server strips it too, for the front door's GET /v1/conversations/<id>.

The rendering case runs in node against a stub DOM and skips where node is
absent, like test_embed_chat.py.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.helpers import ScriptedClient, make_waku, response, text_block
from waku.config import Settings
from waku.db import connect
from waku.memory import tool_note
from waku.memory.consolidation import consolidate_if_due
from waku.memory.episodic.store import SqliteEpisodeStore
from waku.memory.semantic.store import SqliteFactStore
from waku.ops.dashboard import _thread_history, session_list
from waku.runtime.session import Session

JS = Path(__file__).resolve().parents[2] / "waku" / "ops" / "static" / "js"
NODE = shutil.which("node")

QUERY = {"query": "agent memory competitors mem0 zep letta"}
# A treg_catalog_search answer in the shape treg returns: the query, then one
# entry per endpoint with its parameters and price. Thirty is a normal page.
SEARCH_OUTPUT = json.dumps({
    "query": QUERY["query"],
    "results": [{
        "endpoint_id": f"provider-{i}/v1/search",
        "provider": f"provider-{i}",
        "name": f"Search endpoint {i}",
        "description": ("Searches the provider's index of companies, people and web pages "
                        "and returns ranked matches with their source URLs and snippets."),
        "parameters": [{"name": n, "type": "string", "required": n == "q",
                        "description": f"The {n} to send."}
                       for n in ("q", "country", "language", "limit", "offset")],
        "price": {"usd": 0.004, "unit": "call"},
    } for i in range(30)],
}, indent=2)
CALL = {"tool": "treg_catalog_search", "args": QUERY, "output": SEARCH_OUTPUT}
REPLY = "I found three vendors that compete with Mem0."
# The record exactly as add_exchange wrote it before this fix.
OLD_RECORD = f"{REPLY}\n[tools used: {CALL['tool']}({CALL['args']}) -> {CALL['output']}]"


def _session(tmp_path) -> Session:
    return Session(Settings(home=tmp_path))


# --- 1. storage -----------------------------------------------------------------


def test_the_note_keeps_the_tool_and_args_but_not_the_full_output(tmp_path):
    session = _session(tmp_path)
    session.add_exchange("who competes with mem0?", REPLY, tool_calls=[CALL])
    record = session.history[-1]["content"]

    assert record.startswith(REPLY + "\n[tools used: treg_catalog_search(")
    assert QUERY["query"] in record, "the args are kept, so the model knows what it searched"
    assert record.endswith("]")
    assert "provider-29" not in record, "the full output is never stored"
    note = record[len(REPLY) + 1:]
    assert len(note) <= len("[tools used: treg_catalog_search(") + tool_note.ARGS_MAX \
        + tool_note.OUTPUT_MAX + 80, "the note is bounded, whatever the output's size"
    # the measured reduction for one catalog search (reported in the PR)
    assert len(OLD_RECORD) > 20 * len(record), (len(OLD_RECORD), len(record))


def test_a_short_output_is_kept_whole():
    """An error or a status line is the part the model needs; it fits."""
    note = tool_note.note([{"tool": "create_event", "args": {"title": "X"},
                            "output": "error: X already exists at 09:00"}])
    assert note == "[tools used: create_event({'title': 'X'}) -> error: X already exists at 09:00]"


def test_the_record_still_says_which_tools_ran(tmp_path):
    """The note's one job, unchanged: the next turn sees every tool that ran."""
    session = _session(tmp_path)
    session.add_exchange("book it", "Done.", tool_calls=[
        {"tool": "list_events", "args": {}, "output": "nothing on Monday"},
        {"tool": "create_event", "args": {"title": "X"}, "output": "Created X"}])
    record = session.history[-1]["content"]
    assert "list_events(" in record and "-> Created X" in record


def test_no_tools_no_note(tmp_path):
    session = _session(tmp_path)
    session.add_exchange("hi", "Hello.")
    assert session.history[-1]["content"] == "Hello."


def test_an_old_row_is_clipped_when_a_thread_is_reopened(tmp_path, monkeypatch):
    """Rows written before this fix keep their full output in chat_log; switching
    back to that thread must not put it into the model's history again."""
    monkeypatch.setenv("WAKU_HOME", str(tmp_path / "home"))
    app = make_waku(tmp_path / "home", client=ScriptedClient([]))
    app.memory.log_chat("who competes with mem0?", OLD_RECORD, session_id="old")
    app.session.switch("old")
    record = app.session.history[-1]["content"]
    assert record.startswith(REPLY + "\n[tools used: treg_catalog_search(")
    assert len(record) <= len(REPLY) + 1 + tool_note.NOTE_MAX + 40
    assert "provider-29" not in record


def test_consolidation_reads_an_old_row_clipped(tmp_path):
    conn = connect(tmp_path)
    for i in range(3):
        conn.execute("INSERT INTO chat_log (role, content) VALUES ('user', ?)", (f"msg {i}",))
        conn.execute("INSERT INTO chat_log (role, content) VALUES ('assistant', ?)",
                     (OLD_RECORD if i == 0 else f"reply {i}",))
    conn.commit()
    prompts = []

    def create(**kw):
        prompts.append(kw["messages"][0]["content"])
        return response([text_block('{"facts": [], "episode": ""}')])

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    consolidate_if_due(conn, client, "small", 3, SqliteFactStore(conn), SqliteEpisodeStore(conn))
    assert prompts and REPLY in prompts[0] and "treg_catalog_search(" in prompts[0]
    assert "provider-29" not in prompts[0]


# --- 2. rendering -----------------------------------------------------------------


def _seed_old_row(conn, meta: dict | None) -> None:
    conn.execute("INSERT INTO chat_log (role, content, session_id) VALUES ('user', 'q', 't')")
    conn.execute("INSERT INTO chat_log (role, content, session_id, meta) "
                 "VALUES ('assistant', ?, 't', ?)",
                 (OLD_RECORD, json.dumps(meta) if meta else None))
    conn.commit()


def test_the_server_never_returns_the_note(tmp_path):
    conn = connect(tmp_path)
    _seed_old_row(conn, {"tools": [{"tool": "treg_catalog_search", "status": "ok"}]})
    history = _thread_history(conn, "t")
    assert history[1]["content"] == REPLY
    assert session_list(conn)[0]["last"] == "waku: " + REPLY


def test_strip_leaves_a_reply_without_a_note_alone():
    assert tool_note.strip("Paris.") == "Paris."
    assert tool_note.strip("") == ""


_PROGRAM = r"""
const vm = require("vm"), fs = require("fs");
const ctx = {console, JSON, Math, Date, URL};
ctx.window = ctx;
ctx.document = {body: {classList: {contains: () => false}}, documentElement: {dataset: {}},
                getElementById: () => null, querySelectorAll: () => []};
ctx.localStorage = {getItem: () => null, setItem(){}};
vm.createContext(ctx);
for (const f of FILES) vm.runInContext(fs.readFileSync(f, "utf8"), ctx, {filename: f});
const histItem = vm.runInContext("histItem", ctx);
const CHAT = vm.runInContext("CHAT", ctx);
const out = {};
for (const [name, row] of Object.entries(ROWS)) {
  CHAT.length = 0;
  CHAT.push(histItem({role: "user", content: "q"}), histItem(row));
  out[name] = vm.runInContext("renderChatLog", ctx)();
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_loaded_thread_with_a_huge_note_renders_only_the_reply():
    """THE regression: a row with meta took chatTurnCard, which drew the note
    whole. Both kinds of stored row render the reply and nothing of the note."""
    rows = {
        "with_meta": {"role": "assistant", "content": OLD_RECORD,
                      "meta": {"tools": [{"tool": "treg_catalog_search", "status": "ok"}],
                               "iterations": 2, "latency_ms": 900, "model": "m"}},
        "plain": {"role": "assistant", "content": OLD_RECORD, "meta": None},
    }
    files = [str(JS / f) for f in ("util.js", "ui.js", "render.js")]
    program = f"const FILES = {json.dumps(files)};\nconst ROWS = {json.dumps(rows)};\n{_PROGRAM}"
    out = subprocess.run([NODE, "-e", program], capture_output=True, text=True,  # noqa: S603
                         timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    rendered = json.loads(out.stdout.strip().splitlines()[-1])
    for name, html in rendered.items():
        assert "three vendors that compete with Mem0" in html, name
        assert "tools used" not in html, f"{name}: the note was drawn"
        assert "provider-0" not in html and "endpoint_id" not in html, f"{name}: raw output drawn"
        assert len(html) < 8000, f"{name}: {len(html)} characters of HTML for one short reply"
