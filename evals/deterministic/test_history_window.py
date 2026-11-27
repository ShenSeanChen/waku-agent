"""DETERMINISTIC EVAL — working memory is a bounded sliding window.

Sean's insight while testing Telegram: its one always-on session accumulated
history forever, and every turn resent the whole thing (unbounded context ->
cost/latency climb -> eventual context-limit break). Working memory must be a
fixed window; older turns live in state.db + consolidation, not the prompt."""

from __future__ import annotations

from evals.helpers import ScriptedClient, make_waku, response, text_block


def _gate_skip():
    return response([text_block('{"retrieve": false, "query": "", "reason": "t"}')])


def test_prompt_history_is_windowed(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_HISTORY_TURNS", "3")   # keep only last 3 turns
    sent = []

    class Recorder(ScriptedClient):
        def _create(self, **kwargs):
            # snapshot the message count NOW — run_loop mutates the same list
            # (appends the assistant reply) after this call returns
            sent.append(list(kwargs.get("messages", [])))
            return self._script.pop(0)

    # 5 turns; each turn = gate call (skip) + one loop call
    script = []
    for _ in range(5):
        script += [_gate_skip(), response([text_block("ok")])]
    app = make_waku(tmp_path / "home", client=Recorder(script))
    for i in range(5):
        app.respond(f"message number {i}")

    # the LAST loop call's messages: at most 3 turns * 2 rows + the new user
    # message = 7, and it must NOT contain the oldest turns
    last = sent[-1]
    assert len(last) <= 3 * 2 + 1, f"window not applied: {len(last)} messages"
    text_blob = " ".join(str(m.get("content", "")) for m in last)
    assert "message number 0" not in text_blob
    assert "message number 4" in text_blob   # the newest turn is present


def test_default_window_is_generous_but_finite(tmp_path, monkeypatch):
    monkeypatch.delenv("WAKU_HISTORY_TURNS", raising=False)
    app = make_waku(tmp_path / "home", client=ScriptedClient([]))
    assert app.settings.history_turns == 12


# --- spec 039 A3: each earlier answer is capped in the window --------------------------

LONG = "Pricing notes. " * 400          # 6,000 characters


def _two_turns(tmp_path, monkeypatch, cap: str | None):
    if cap is None:
        monkeypatch.delenv("WAKU_HISTORY_ANSWER_CHARS", raising=False)
    else:
        monkeypatch.setenv("WAKU_HISTORY_ANSWER_CHARS", cap)
    sent = []

    class Recorder(ScriptedClient):
        def _create(self, **kwargs):
            if kwargs.get("tools"):
                sent.append(list(kwargs.get("messages", [])))
            return self._script.pop(0)

    script = [_gate_skip(), response([text_block(LONG)]),
              _gate_skip(), response([text_block("ok")])]
    app = make_waku(tmp_path / "home", client=Recorder(script))
    app.respond("what did I decide about pricing?")
    app.respond("and last week?")
    earlier = [m["content"] for m in sent[-1] if m["role"] == "assistant"]
    logged = [r[0] for r in app.conn.execute(
        "SELECT content FROM chat_log WHERE role = 'assistant' ORDER BY id")]
    return earlier, logged


def test_an_earlier_long_answer_enters_the_window_cut_and_the_chat_log_keeps_it_whole(
        tmp_path, monkeypatch):
    earlier, logged = _two_turns(tmp_path, monkeypatch, "1500")
    assert len(earlier) == 1
    assert len(earlier[0]) < 1700
    assert earlier[0].startswith("Pricing notes.")
    assert "more characters in the chat log" in earlier[0]
    assert logged[0] == LONG.strip() or logged[0] == LONG


def test_without_a_cap_an_earlier_answer_enters_whole(tmp_path, monkeypatch):
    """A laptop's default: no cap, as before spec 039."""
    earlier, _ = _two_turns(tmp_path, monkeypatch, None)
    assert earlier == [LONG] or earlier == [LONG.strip()]


def test_the_cap_keeps_the_tools_used_note():
    """The note says a tool already ran; cutting it would let the model run
    the tool again (the triple-booked meeting)."""
    from waku.memory import tool_note

    note = tool_note.note([{"tool": "create_event", "args": {"title": "X"}, "output": "Created X"}])
    capped = tool_note.cap_reply(f"{'a' * 3000}\n{note}", 1500)
    assert capped.endswith(note)
    assert capped.startswith("a" * 1500)
    assert "a" * 1501 not in capped
    assert tool_note.cap_reply("short\n" + note, 1500) == "short\n" + note
