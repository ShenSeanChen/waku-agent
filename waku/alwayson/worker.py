"""The one worker: it claims events, batches them per session and starts one
run for each batch (spec 023, decision 2).

  claim the oldest pending row
  wait 3 seconds, so a second message sent right after the first joins it
  claim every pending row of that session
  one run with all of them, through waku.respond()
  write the run's reply into the outbox, unless the run chose to say nothing
  mark the rows done, or failed when the run raised

One worker per home. Two workers on one state.db would each start a run for
the same conversation and the person would get two answers, so a second
`waku serve` refuses to start (ServeLock).

At startup the worker returns every claimed row to pending (recover). A row is
only claimed while a run is in flight, so a claimed row at startup belongs to a
run that died, and that run happens again. Its reply carries the same
idempotency key as the first time, so the outbox keeps one row and the person
gets one reply.

The worker never sends. The dispatcher sends what the outbox holds.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from waku.alwayson import events, outbox
from waku.alwayson.events import Event

BATCH_WAIT_SECONDS = 3.0
IDLE_SECONDS = 0.5
LOCK_NAME = "serve.lock"


class AlreadyRunning(RuntimeError):
    """Another `waku serve` holds this home's lock."""


class ServeLock:
    """An operating-system lock on <home>/serve.lock. The system releases it
    when its holder exits, however it exits, so a crash never leaves a stale
    lock that someone has to delete by hand."""

    def __init__(self, home: Path) -> None:
        self.path = home / LOCK_NAME
        self._file = None

    def acquire(self) -> None:
        handle = open(self.path, "a+")  # noqa: SIM115  (held until release)
        try:
            _lock(handle)
        except OSError:
            handle.close()
            raise AlreadyRunning(
                f"waku serve is already running on {self.path.parent}. Two workers on one "
                "state.db would answer the same message twice. Stop the other one first."
            ) from None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._file = handle

    def release(self) -> None:
        if self._file is not None:
            self._file.close()   # closing the file releases the lock
            self._file = None


def _lock(handle) -> None:
    """Take an exclusive lock without waiting; OSError when someone holds it."""
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def run_id(batch: list[Event]) -> str:
    """The name of the run a batch starts: `e` and the id of its first event.
    A run that is replayed after a crash starts with the same first event, so
    it gets the same name and writes the same idempotency keys."""
    return f"e{batch[0].id}"


def is_silent(reply: str | None) -> bool:
    """A run may end without saying anything: an empty reply, or the single
    word SILENT."""
    return (reply or "").strip().strip(".!").upper() in ("", "SILENT")


def reply_address(batch: list[Event]) -> tuple[str, dict] | None:
    """Where the reply to a batch goes: the newest event that says where a
    reply goes decides, and the channel is the one that event came in on."""
    for event in reversed(batch):
        target = event.payload.get("reply_to")
        if target and event.source in outbox.CHANNELS:
            return event.source, target
    return None


def render(batch: list[Event]) -> str:
    """The one user message a run starts with: each event's text, oldest first."""
    return "\n\n".join(e.payload.get("text", "") for e in batch).strip()


class AgentRun:
    """One run: a batch of events becomes one waku.respond() turn in the
    session the events name. The agent is built on first use, on the worker's
    thread, because Waku owns a SQLite connection that stays on one thread."""

    def __init__(self, agent_factory: Callable[[], Any], observer=None,
                 log: Callable[[str], None] = print) -> None:
        self._factory, self._observer, self._log = agent_factory, observer, log
        self._agent: Any | None = None

    def __call__(self, session_id: str, batch: list[Event]) -> str:
        if self._agent is None:
            self._agent = self._factory()
        session = self._agent.session
        if session.session_id != session_id:
            session.switch(session_id)   # load that conversation's recent turns
        text = render(batch)
        self._log(f"{session_id} › {text}")
        result = self._agent.respond(text, observer=self._observer, source=batch[0].source)
        self._log(f"waku › {result.reply}")
        return result.reply

    def close(self) -> None:
        if self._agent is None:
            return
        agent, self._agent = self._agent, None
        try:
            agent.close()
        finally:
            if getattr(agent, "conn", None) is not None:
                agent.conn.close()


class Worker:
    def __init__(self, conn: sqlite3.Connection,
                 run: Callable[[str, list[Event]], str | None], *,
                 batch_wait: float = BATCH_WAIT_SECONDS,
                 wait: Callable[[float], Any] | None = None,
                 now: Callable[[], datetime] = outbox.utc_now,
                 log: Callable[[str], None] = print) -> None:
        self.conn, self._run, self.batch_wait, self._log = conn, run, batch_wait, log
        self._now = now
        self.stop = threading.Event()
        # stop.wait returns early when `waku serve` is told to stop. The evals
        # pass their own wait, so no test sleeps for real seconds.
        self._wait = wait or self.stop.wait

    def recover(self) -> int:
        return events.recover(self.conn)

    def step(self) -> bool:
        """Run one batch. False when there was nothing to run."""
        first = events.claim_next(self.conn)
        if first is None:
            return False
        self._wait(self.batch_wait)
        batch = [first, *events.claim_session(self.conn, first.session_id)]
        ids = [e.id for e in batch]
        if self.stop.is_set():
            events.mark(self.conn, ids, "pending")   # the next start runs them
            return False
        try:
            reply = self._run(first.session_id, batch)
        except Exception as exc:
            from waku.gateway.runner import safe_exception

            events.mark(self.conn, ids, "failed")
            self._log(f"(serve) the run for {first.session_id} failed: {safe_exception(exc)}")
        else:
            self._queue_reply(batch, reply)
            events.mark(self.conn, ids, "done")
        return True

    def _queue_reply(self, batch: list[Event], reply: str | None) -> None:
        """The run's reply becomes one outbox row. Its key is `<run>:0`."""
        if is_silent(reply):
            return
        address = reply_address(batch)
        if address is None:
            self._log(f"(serve) the run for {batch[0].session_id} has nowhere to reply")
            return
        channel, target = address
        run = run_id(batch)
        outbox.insert(self.conn, run_id=run, channel=channel, target=target,
                      body=reply.strip(), idempotency_key=f"{run}:0", now=self._now())

    def run_forever(self, idle: float = IDLE_SECONDS) -> None:
        recovered = self.recover()
        if recovered:
            self._log(f"(serve) {recovered} message(s) from a run that did not finish "
                      "will run again")
        while not self.stop.is_set():
            if not self.step():
                self.stop.wait(idle)
