"""DETERMINISTIC EVAL: `waku serve` (spec 023, task A3).

One process runs the handlers and the worker, refuses to start twice on one
home, listens on 127.0.0.1 unless told otherwise, and stops cleanly on SIGTERM.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from waku.alwayson import serve
from waku.alwayson.events import Inbox
from waku.alwayson.worker import AlreadyRunning, ServeLock
from waku.config import Settings
from waku.db import connect

REPO = Path(__file__).resolve().parents[2]


def _settings(tmp_path) -> Settings:
    settings = Settings(home=tmp_path)
    settings.ensure_home()
    return settings


def _until(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def _service(tmp_path, **kwargs):
    kwargs.setdefault("run", lambda session_id, batch: None)
    kwargs.setdefault("batch_wait", 0)
    kwargs.setdefault("log", lambda line: None)
    return serve.Service(_settings(tmp_path), **kwargs)


def test_the_command_reaches_serve(monkeypatch):
    import waku.__main__ as entry

    called = []
    monkeypatch.setattr(serve, "main", lambda argv: called.append(argv) or 0)
    monkeypatch.setattr(sys, "argv", ["waku", "serve", "--port", "9000"])
    with pytest.raises(SystemExit) as done:
        entry.main()
    assert done.value.code == 0
    assert called == [["--port", "9000"]]


def test_the_help_lists_the_command(monkeypatch, capsys):
    import waku.__main__ as entry

    monkeypatch.setattr(sys, "argv", ["waku", "no-such-command"])
    with pytest.raises(SystemExit):
        entry.main()
    assert "waku serve" in capsys.readouterr().out


def test_an_event_inserted_while_serving_becomes_a_run(tmp_path):
    runs = []
    service = _service(tmp_path, run=lambda session_id, batch: runs.append(
        (session_id, [e.payload["text"] for e in batch])))
    service.start()
    try:
        Inbox(tmp_path).add(source="telegram", external_id="1",
                            session_id="telegram:42", payload={"text": "hello"})
        assert _until(lambda: runs), "the worker never ran the event"
    finally:
        service.stop()
    assert runs == [("telegram:42", ["hello"])]
    conn = connect(tmp_path)
    try:
        assert [r["status"] for r in conn.execute("SELECT status FROM events")] == ["done"]
    finally:
        conn.close()


def test_an_outbox_row_written_while_serving_is_sent(tmp_path):
    """Task D2: the dispatcher runs inside `waku serve`."""
    from waku.alwayson import outbox

    sent = []
    service = _service(tmp_path, senders={
        "telegram": lambda message: sent.append((message.target, message.body))})
    service.start()
    try:
        conn = connect(tmp_path)
        try:
            outbox.insert(conn, run_id="e1", channel="telegram", target={"chat_id": 42},
                          body="hello Sean", idempotency_key="e1:0")
            assert _until(lambda: sent), "the dispatcher never sent the row"
            assert _until(lambda: [r["status"] for r in conn.execute(
                "SELECT status FROM outbox")] == ["sent"])
        finally:
            conn.close()
    finally:
        service.stop()
    assert sent == [({"chat_id": 42}, "hello Sean")]


def test_a_message_to_serve_gets_its_reply_on_the_channel_it_came_from(tmp_path):
    """Task D3: handler, worker, outbox and dispatcher in one process."""
    sent = []
    service = _service(
        tmp_path, run=lambda session_id, batch: f"You said: {batch[0].payload['text']}",
        senders={"telegram": lambda message: sent.append((message.target, message.body))})
    service.start()
    try:
        Inbox(tmp_path).add(source="telegram", external_id="1", session_id="telegram:42",
                            payload={"text": "hello", "reply_to": {"chat_id": 42}})
        assert _until(lambda: sent), "the reply never reached the sender"
    finally:
        service.stop()
    assert sent == [({"chat_id": 42}, "You said: hello")]


def test_stop_ends_the_worker_and_releases_the_lock(tmp_path):
    service = _service(tmp_path)
    service.start()
    thread = service.worker_thread
    assert thread.is_alive()
    assert service.dispatcher_thread.is_alive()
    with pytest.raises(AlreadyRunning):
        ServeLock(tmp_path).acquire()

    dispatcher = service.dispatcher_thread
    service.stop()

    assert not thread.is_alive()
    assert not dispatcher.is_alive()
    lock = ServeLock(tmp_path)
    lock.acquire()
    lock.release()


def test_a_second_serve_on_the_same_home_refuses_to_start(tmp_path, capsys, monkeypatch):
    first = _service(tmp_path)
    first.start()
    try:
        with pytest.raises(AlreadyRunning):
            _service(tmp_path).start()
        monkeypatch.setenv("WAKU_HOME", str(tmp_path))
        assert serve.main([]) == 1
        assert "already running" in capsys.readouterr().out
    finally:
        first.stop()


def test_sources_start_with_the_inbox_and_stop_with_the_service(tmp_path):
    seen = []

    class Handle:
        def stop(self):
            seen.append("stopped")

    def source(inbox):
        seen.append(inbox.home)
        return Handle()

    def not_configured(inbox):
        return None

    def broken(inbox):
        raise RuntimeError("bad token")

    logged = []
    service = _service(tmp_path, sources={"fake": source, "off": not_configured,
                                          "broken": broken}, log=logged.append)
    service.start()
    try:
        assert seen == [tmp_path]
        assert service.running == ["fake"]
        assert service.worker_thread.is_alive(), "one broken source does not stop the rest"
        assert any("broken" in line and "RuntimeError" in line for line in logged)
    finally:
        service.stop()
    assert seen == [tmp_path, "stopped"]


def test_the_sources_line_names_a_source_whose_extra_is_missing(tmp_path):
    """Found in a live run: TELEGRAM_BOT_TOKEN was set, the extra was not
    installed, and the line that lists the sources said only `github`. The
    reason was printed once, far above it."""
    from waku.alwayson.sources import MissingExtra

    class Handle:
        def stop(self):
            pass

    def needs_extra(inbox):
        raise MissingExtra("TELEGRAM_BOT_TOKEN", "telegram")

    logged = []
    service = _service(tmp_path, log=logged.append, sources={
        "telegram": needs_extra, "slack": lambda inbox: None,
        "github": lambda inbox: Handle()})
    service.start()
    try:
        line = serve.sources_line(service)
    finally:
        service.stop()

    assert line == ("  sources: github. Not running: telegram (TELEGRAM_BOT_TOKEN is set, "
                    "but the extra is not installed: pip install 'waku-agent[telegram]')")
    assert service.missing == {}, "stop clears what start found"


def test_the_sources_line_when_the_only_configured_source_lacks_its_extra(tmp_path):
    from waku.alwayson.sources import MissingExtra

    def needs_extra(inbox):
        raise MissingExtra("SLACK_APP_TOKEN", "slack")

    service = _service(tmp_path, sources={"slack": needs_extra})
    service.start()
    try:
        line = serve.sources_line(service)
    finally:
        service.stop()

    assert line.startswith("  sources: none running. Not running: slack (SLACK_APP_TOKEN")
    assert "pip install 'waku-agent[slack]'" in line


def test_the_sources_line_without_any_configured_source(tmp_path):
    service = _service(tmp_path, sources={"slack": lambda inbox: None})
    service.start()
    try:
        assert serve.sources_line(service) == (
            "  sources: none configured (docs/integrations.md lists them)")
    finally:
        service.stop()


def test_the_listener_binds_to_loopback_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("WAKU_SERVE_HOST", raising=False)
    service = _service(tmp_path, port=0,
                       routes={"/webhooks/test": lambda headers, body: (200, b'{"got": true}')})
    service.start()
    try:
        host, port = service.address
        assert host == "127.0.0.1"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as reply:
            assert json.loads(reply.read()) == {"ok": True}
        request = urllib.request.Request(f"http://127.0.0.1:{port}/webhooks/test",
                                         data=b"{}", method="POST")
        with urllib.request.urlopen(request, timeout=5) as reply:
            assert json.loads(reply.read()) == {"got": True}
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{port}/webhooks/other", data=b"{}", method="POST"), timeout=5)
        assert missing.value.code == 404
    finally:
        service.stop()


def test_no_listener_without_a_route(tmp_path):
    """Nothing listens on a port unless something is configured to receive."""
    service = _service(tmp_path)
    service.start()
    try:
        assert service.address is None
    finally:
        service.stop()


def test_a_body_over_the_limit_is_refused(tmp_path):
    called = []
    service = _service(tmp_path, port=0,
                       routes={"/webhooks/test": lambda h, b: called.append(b) or (200, b"{}")})
    service.start()
    try:
        port = service.address[1]
        # Announce a body one byte over the limit and send none of it: the
        # refusal has to come from the header, before anything is read.
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(b"POST /webhooks/test HTTP/1.1\r\nHost: localhost\r\n"
                           b"Content-Length: 1000001\r\n\r\n")
            status_line = client.recv(4096).split(b"\r\n", 1)[0]
        assert status_line.split()[1] == b"413"
        assert called == []
    finally:
        service.stop()


def test_the_host_variable_changes_the_bind_and_warns(monkeypatch, capsys):
    monkeypatch.delenv("WAKU_SERVE_HOST", raising=False)
    assert serve.bind_host() == "127.0.0.1"
    assert capsys.readouterr().out == ""

    monkeypatch.setenv("WAKU_SERVE_HOST", "0.0.0.0")
    assert serve.bind_host() == "0.0.0.0"
    warning = capsys.readouterr().out
    assert "WAKU_SERVE_HOST" in warning and "0.0.0.0" in warning


@pytest.mark.skipif(os.name == "nt", reason="SIGTERM is a POSIX signal")
def test_sigterm_stops_the_process_cleanly(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "WAKU_HOME": str(home),
           "PYTHONPATH": str(REPO), "PYTHONUNBUFFERED": "1"}
    process = subprocess.Popen([sys.executable, "-m", "waku", "serve"], cwd=tmp_path, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert _until(lambda: (home / "serve.lock").exists()
                      and (home / "serve.lock").read_text().strip() == str(process.pid), 30), (
            "waku serve never took its lock")
        with pytest.raises(AlreadyRunning):
            ServeLock(home).acquire()
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode == 0, output
    assert "Traceback" not in output, output
    assert "waku serve stopped" in output
    lock = ServeLock(home)
    lock.acquire()   # the process let go of it
    lock.release()
