"""DETERMINISTIC EVAL: the two routes a hosted container answers for the
gateway, and the always-on service inside the dashboard (spec 026, task A2).

  POST /api/events          the gateway hands a tenant's waiting events over
  GET  /api/alwayson/next   the gateway asks what work is left before a stop

Both exist only when WAKU_HOSTED=1. Offline: a real dashboard server on a free
loopback port, a temp home, and a run that is a plain function.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer

import pytest

from waku.alwayson import events, handover, outbox, runs, timers
from waku.alwayson.worker import AlreadyRunning, ServeLock
from waku.config import Settings
from waku.db import connect
from waku.ops import dashboard

EVENT = {"source": "slack", "external_id": "Ev0001", "session_id": "slack:C1:171.5",
         "payload": {"text": "remind the channel on Friday at 09:00",
                     "reply_to": {"channel": "C1", "thread_ts": "171.5"}}}
FRIDAY = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
GATEWAY_ENV = ("TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "WHATSAPP_TOKEN", "SLACK_BOT_TOKEN",
               "SLACK_APP_TOKEN", "GITHUB_WEBHOOK_SECRET", "WAKU_OWNER")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_HOME", str(tmp_path))
    monkeypatch.delenv("WAKU_DASHBOARD_HOST", raising=False)
    for name in GATEWAY_ENV:
        monkeypatch.delenv(name, raising=False)
    Settings(home=tmp_path).ensure_home()
    return tmp_path


@pytest.fixture
def server(home):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_port
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setenv("WAKU_HOSTED", "1")


def _call(port: int, method: str, path: str, body=None) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    raw = json.dumps(body) if body is not None else None
    conn.request(method, path, body=raw, headers={"Content-Type": "application/json"})
    response = conn.getresponse()
    text = response.read().decode()
    conn.close()
    return response.status, (json.loads(text) if text else {})


def _rows(home) -> list[events.Event]:
    conn = connect(home)
    try:
        return [events._event(row) for row in conn.execute("SELECT * FROM events ORDER BY id")]
    finally:
        conn.close()


def _until(condition, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


# --- POST /api/events -------------------------------------------------------

def test_a_handed_event_is_one_pending_row_in_the_inbox(server, home, hosted):
    status, answer = _call(server, "POST", "/api/events", {"events": [EVENT]})

    assert (status, answer) == (200, {"accepted": 1, "duplicates": 0})
    (row,) = _rows(home)
    assert (row.source, row.external_id, row.session_id) == ("slack", "Ev0001",
                                                             "slack:C1:171.5")
    assert row.payload == EVENT["payload"]
    assert row.status == "pending"


def test_an_event_handed_twice_is_one_row_and_still_answers_200(server, home, hosted):
    """The gateway marks a row handed only after a 200. If that write is lost
    it hands the row again, and the second hand-over must succeed without a
    second row."""
    _call(server, "POST", "/api/events", {"events": [EVENT]})
    status, answer = _call(server, "POST", "/api/events",
                           {"events": [EVENT, {**EVENT, "external_id": "Ev0002"}]})

    assert (status, answer) == (200, {"accepted": 1, "duplicates": 1})
    assert [row.external_id for row in _rows(home)] == ["Ev0001", "Ev0002"]


def test_the_route_does_not_exist_on_a_persons_own_machine(server, home, monkeypatch):
    monkeypatch.delenv("WAKU_HOSTED", raising=False)
    status, _ = _call(server, "POST", "/api/events", {"events": [EVENT]})

    assert status == 404
    assert _rows(home) == []


@pytest.mark.parametrize("value", ["0", "true", "yes", ""])
def test_only_the_value_1_turns_the_routes_on(server, home, monkeypatch, value):
    monkeypatch.setenv("WAKU_HOSTED", value)
    assert _call(server, "POST", "/api/events", {"events": [EVENT]})[0] == 404
    assert _call(server, "GET", "/api/alwayson/next")[0] == 404
    assert _rows(home) == []


@pytest.mark.parametrize("body", [
    {},
    {"events": []},
    {"events": "Ev0001"},
    {"events": [{**EVENT, "source": ""}]},
    {"events": [{**EVENT, "external_id": ""}]},
    {"events": [{**EVENT, "session_id": ""}]},
    {"events": [{**EVENT, "payload": "text"}]},
    {"events": [{k: v for k, v in EVENT.items() if k != "payload"}]},
    {"events": [EVENT, "not an event"]},
])
def test_a_malformed_hand_over_is_refused_whole(server, home, hosted, body):
    """All or nothing: a batch with one bad event writes no row, so the
    gateway's rows stay pending together and are handed again together."""
    status, answer = _call(server, "POST", "/api/events", body)

    assert status == 400
    assert "error" in answer
    assert _rows(home) == []


@pytest.mark.parametrize("source", ["timer", "approval"])
def test_an_event_cannot_claim_a_source_the_container_writes_itself(server, home, hosted,
                                                                    source):
    """A run reads a `timer` event as its own note to itself and an `approval`
    event as the outcome of a call the owner answered. Neither can arrive from
    outside."""
    status, _ = _call(server, "POST", "/api/events", {"events": [{**EVENT, "source": source}]})

    assert status == 400
    assert _rows(home) == []


# --- GET /api/alwayson/next -------------------------------------------------

def test_a_container_with_nothing_left_reports_nothing(server, home, hosted):
    status, answer = _call(server, "GET", "/api/alwayson/next")

    assert status == 200
    assert answer == {"next_timer_at": None, "events": 0, "outbox": 0, "running": 0}


def test_the_earliest_pending_timer_is_reported(server, home, hosted):
    conn = connect(home)
    later = FRIDAY + timedelta(days=3)
    for due, key in ((later, "k-later"), (FRIDAY, "k-friday")):
        timers.insert(conn, due_at=due, session_id="slack:C1:171.5", note="post the reminder",
                      address=None, idempotency_key=key)
    fired = timers.insert(conn, due_at=FRIDAY - timedelta(days=7), session_id="s", note="old",
                          address=None, idempotency_key="k-fired")
    timers.mark_fired(conn, fired)
    conn.close()

    _, answer = _call(server, "GET", "/api/alwayson/next")
    assert answer["next_timer_at"] == "2026-11-20T09:00:00+00:00"


def test_an_unsent_reply_an_unread_event_and_a_run_in_flight_are_counted(server, home, hosted):
    conn = connect(home)
    sent = outbox.insert(conn, run_id="1", channel="slack", target={"channel": "C1"},
                         body="done", idempotency_key="1:0")
    outbox.mark_sent(conn, sent)
    outbox.insert(conn, run_id="1", channel="slack", target={"channel": "C1"},
                  body="still to send", idempotency_key="1:1")
    first = events.insert(conn, source="slack", external_id="Ev1", session_id="s", payload={})
    events.insert(conn, source="slack", external_id="Ev2", session_id="s", payload={})
    events.insert(conn, source="slack", external_id="Ev3", session_id="s", payload={})
    events.mark(conn, [first], "done")
    events.claim_next(conn)
    finished = runs.start(conn, "s", [first])
    runs.finish(conn, finished.id, "done")
    runs.start(conn, "s2", [first + 1])
    conn.close()

    _, answer = _call(server, "GET", "/api/alwayson/next")
    assert answer == {"next_timer_at": None, "events": 2, "outbox": 1, "running": 1}


def test_a_parked_run_is_not_work_in_flight(server, home, hosted):
    """A run waiting for the owner's yes is a row. The yes arrives as an
    event, so the container may stop in between."""
    conn = connect(home)
    first = events.insert(conn, source="slack", external_id="Ev1", session_id="s", payload={})
    events.mark(conn, [first], "done")
    run = runs.start(conn, "s", [first])
    runs.finish(conn, run.id, "parked")
    conn.close()

    _, answer = _call(server, "GET", "/api/alwayson/next")
    assert answer["running"] == 0


# --- the worker, timer loop and dispatcher inside the dashboard -------------

class _FakeServer:
    """Stands in for the dashboard's HTTP server so main() returns. What the
    test wants to see happens while main() believes it is serving."""

    during = staticmethod(lambda: None)

    def __init__(self, *args, **kwargs) -> None:
        pass

    def serve_forever(self) -> None:
        type(self).during()


def _run_main(monkeypatch, during) -> None:
    monkeypatch.setattr(_FakeServer, "during", staticmethod(during))
    monkeypatch.setattr(dashboard, "ThreadingHTTPServer", _FakeServer)
    dashboard.main()


def test_a_hosted_dashboard_runs_the_event_it_was_handed(home, hosted, monkeypatch, capsys):
    seen: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(handover, "BATCH_WAIT_SECONDS", 0)
    monkeypatch.setattr(handover, "_agent_run", lambda settings, log: (
        lambda session_id, batch: seen.append((session_id, [e.external_id for e in batch]))))
    observed = {}

    def during() -> None:
        conn = connect(home)
        handover.accept(conn, {"events": [EVENT]})
        observed["ran"] = _until(lambda: bool(seen))
        observed["done"] = _until(lambda: events.get(conn, 1).status == "done")
        conn.close()

    _run_main(monkeypatch, during)

    assert observed == {"ran": True, "done": True}
    assert seen == [("slack:C1:171.5", ["Ev0001"])]
    # main() stopped the service on its way out: the home's lock is free again.
    lock = ServeLock(home)
    lock.acquire()
    lock.release()


def test_a_hosted_dashboard_fires_a_timer_that_came_due_while_it_was_stopped(
        home, hosted, monkeypatch, capsys):
    """Acceptance 3's second half: the gateway starts the container at the
    timer's time, and the container's own loop turns the timer into a run."""
    conn = connect(home)
    timers.insert(conn, due_at=datetime.now(UTC) - timedelta(minutes=5),
                  session_id="slack:C1:171.5", note="post the reminder", address=None,
                  idempotency_key="k1")
    conn.close()
    seen: list[str] = []
    monkeypatch.setattr(handover, "BATCH_WAIT_SECONDS", 0)
    monkeypatch.setattr(handover, "_agent_run", lambda settings, log: (
        lambda session_id, batch: seen.extend(e.source for e in batch)))

    _run_main(monkeypatch, lambda: _until(lambda: bool(seen)))

    assert seen == ["timer"]


def test_a_dashboard_on_a_persons_own_machine_starts_no_worker(home, monkeypatch, capsys):
    monkeypatch.delenv("WAKU_HOSTED", raising=False)
    conn = connect(home)
    events.insert(conn, source="slack", external_id="Ev1", session_id="s", payload={})
    conn.close()
    observed = {}

    def during() -> None:
        lock = ServeLock(home)
        try:
            lock.acquire()          # free: nothing inside the dashboard holds it
            observed["lock_free"] = True
            lock.release()
        except AlreadyRunning:
            observed["lock_free"] = False
        time.sleep(0.2)

    _run_main(monkeypatch, during)

    assert observed == {"lock_free": True}
    assert [row.status for row in _rows(home)] == ["pending"]
