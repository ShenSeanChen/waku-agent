"""DETERMINISTIC EVAL: a hosted container is started again for each day of a
repeating timer (spec 033).

A hosted container stops when nobody uses it. Before it stops, the gateway
asks it for its earliest timer (GET /api/alwayson/next) and stores that time
in tenant_wakeups, and the scheduler starts the container at that time (spec
026). A repeating timer is one row whose due time moves to the next day when
it fires. This eval hands the real report of a real state.db to the gateway's
reader and its store, on Friday after the brief fired, and again on Saturday.

It reads both sides of the licence line, as the other evals in this folder do:
waku/alwayson (MIT) writes the report, hosted/ (Elastic License 2.0) reads it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from hosted.core import idle
from hosted.gateway.store import ControlDb
from waku.alwayson import handover, timers
from waku.db import connect

ANA = "anaanaanaana"   # a tenant id is twelve letters
FRIDAY_9 = datetime(2026, 11, 20, 9, 0, tzinfo=ZoneInfo("Europe/Madrid"))


def test_the_gateway_stores_the_next_day_after_each_day_fired(tmp_path):
    (tmp_path / "home").mkdir()
    conn = connect(tmp_path / "home")
    store = ControlDb(tmp_path / "control.db")
    try:
        timers.insert(conn, due_at=FRIDAY_9, session_id="slack:C1:1700.0001",
                      note="Daily brief", address=None, idempotency_key="e1:t1",
                      repeat="daily", zone="Europe/Madrid")
        for day in (0, 1):
            morning = FRIDAY_9 + timedelta(days=day)
            loop = timers.TimerLoop(conn, now=lambda at=morning: at, log=lambda line: None)
            assert loop.step() == 1
            conn.execute("UPDATE events SET status = 'done'")   # the run finished
            conn.commit()

            left = idle.work_left(handover.next_work(conn))

            next_morning = datetime(2026, 11, 21 + day, 8, 0, tzinfo=UTC).timestamp()
            assert left == idle.WorkLeft(busy=False, wakeup=next_morning)
            store.set_wakeup(ANA, left.wakeup)
            assert store.due_wakeups(next_morning - 1) == []
            assert store.due_wakeups(next_morning) == [ANA]
    finally:
        conn.close()
        store.close()
