"""Always-on inside a hosted container (spec 026).

A hosted container stops when nobody has used it for a while, and a gateway
outside it keeps running. Three things here let the container's always-on
parts survive that stop. All three are off unless WAKU_HOSTED=1, and none of
them knows anything about the gateway beyond the two requests it makes.

  accept     POST /api/events: the gateway hands over events it kept while the
             container was stopped. Each becomes one row in `events`, exactly
             as a source under `waku serve` would write it.
  next_work  GET /api/alwayson/next: the gateway asks, before it stops the
             container, when the earliest timer is due and whether a run, an
             event or an outgoing message still waits.
  start      the worker, the timer loop and the dispatcher inside the
             dashboard process, so a container runs one process.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable

from waku.alwayson import events
from waku.alwayson.serve import Service, _agent_run
from waku.alwayson.worker import BATCH_WAIT_SECONDS, INTERNAL
from waku.config import Settings

ENV = "WAKU_HOSTED"
_FIELDS = ("source", "external_id", "session_id")


def enabled() -> bool:
    """True in a hosted container, which is started with WAKU_HOSTED=1."""
    return os.getenv(ENV, "") == "1"


def _problem(item: object) -> str:
    """Why one handed event cannot be stored, or "" when it can."""
    if not isinstance(item, dict):
        return "an event must be an object"
    for field in _FIELDS:
        if not isinstance(item.get(field), str) or not item[field]:
            return f"an event needs a non-empty string '{field}'"
    if not isinstance(item.get("payload"), dict):
        return "an event needs an object 'payload'"
    if item["source"] in INTERNAL:
        return f"an event cannot have the source '{item['source']}'"
    return ""


def accept(conn: sqlite3.Connection, body: object) -> tuple[int, dict]:
    """Store the events of one hand-over. Returns the HTTP status and answer.

    The body is {"events": [{source, external_id, session_id, payload}]}. A
    body with one event that cannot be stored writes no row and answers 400.
    An event this inbox already holds is counted as a duplicate and the
    answer is still 200, so the gateway can hand a row over twice."""
    items = body.get("events") if isinstance(body, dict) else None
    if not isinstance(items, list) or not items:
        return 400, {"error": "the body needs a non-empty list 'events'"}
    for item in items:
        if problem := _problem(item):
            return 400, {"error": problem}
    accepted = 0
    for item in items:
        stored = events.insert(conn, source=item["source"], external_id=item["external_id"],
                               session_id=item["session_id"], payload=item["payload"])
        accepted += stored is not None
    return 200, {"accepted": accepted, "duplicates": len(items) - accepted}


def next_work(conn: sqlite3.Connection) -> dict:
    """What this container still has to do.

      next_timer_at  when the earliest pending timer is due (UTC, ISO 8601),
                     or None. A repeating timer stays pending with its next
                     occurrence as its due time (spec 033), so after today's
                     brief fired this is tomorrow's, and the gateway starts
                     the container again for it
      events         events no run has finished with yet
      outbox         outgoing messages not sent yet
      running        runs in flight

    A parked run is not counted: it waits for the owner's answer, and that
    answer arrives as an event."""
    def one(sql: str):
        return conn.execute(sql).fetchone()[0]

    return {
        "next_timer_at": one("SELECT MIN(due_at) FROM timers WHERE status = 'pending'"),
        "events": one("SELECT COUNT(*) FROM events WHERE status IN ('pending', 'claimed')"),
        "outbox": one("SELECT COUNT(*) FROM outbox WHERE status = 'pending'"),
        "running": one("SELECT COUNT(*) FROM runs WHERE status = 'running'"),
    }


def start(settings: Settings, log: Callable[[str], None] = print) -> Service | None:
    """Start the worker, the timer loop and the dispatcher in this process, or
    return None outside a hosted container. No source starts here: a hosted
    container receives every event through `accept`. The caller stops the
    service it gets back."""
    if not enabled():
        return None
    from waku.alwayson import senders

    service = Service(settings, senders=senders.configured(settings),
                      run=_agent_run(settings, log), batch_wait=BATCH_WAIT_SECONDS, log=log)
    service.start()
    return service
