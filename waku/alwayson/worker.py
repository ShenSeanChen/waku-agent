"""The one worker: it claims events, batches them per session and starts one
run for each batch (spec 023, decision 2).

  claim the oldest pending row (a message, or a timer that came due)
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

from waku.alwayson import events, outbox, runs
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


def is_silent(reply: str | None) -> bool:
    """A run may end without saying anything: an empty reply, or the single
    word SILENT."""
    return (reply or "").strip().strip(".!").upper() in ("", "SILENT")


def reply_address(batch: list[Event]) -> tuple[str, dict] | None:
    """Where the reply to a batch goes: the newest event that says where a
    reply goes decides. The channel is the one that event came in on. A timer
    comes in on no channel, so its event names the channel of the conversation
    that set it."""
    for event in reversed(batch):
        target = event.payload.get("reply_to")
        channel = event.payload.get("channel") if event.source == "timer" else event.source
        if target and channel in outbox.CHANNELS:
            return channel, target
    return None


# What a run reads when a timer woke it. Nobody is waiting for an answer, so
# the model is told that it may end the run without sending one.
TIMER_PROMPT = """\
[A timer you set with schedule_task on {set_at} is due now. Nobody sent this \
message. Your note to yourself was:]

{note}

[Do what the note asks. Your reply is sent to the conversation that set the \
timer. If there is nothing worth sending, reply with the single word SILENT \
and nothing is sent.]"""


def _text(event: Event) -> str:
    text = event.payload.get("text", "")
    if event.source != "timer":
        return text
    return TIMER_PROMPT.format(set_at=event.payload.get("set_at", "an earlier day"), note=text)


def render(batch: list[Event]) -> str:
    """The one user message a run starts with: each event's text, oldest first."""
    return "\n\n".join(_text(e) for e in batch).strip()


class AgentRun:
    """One run: a batch of events becomes one waku.respond() turn in the
    session the events name. The agent is built on first use, on the worker's
    thread, because Waku owns a SQLite connection that stays on one thread."""

    def __init__(self, agent_factory: Callable[[], Any], observer=None,
                 log: Callable[[str], None] = print) -> None:
        self._factory, self._observer, self._log = agent_factory, observer, log
        self._agent: Any | None = None

    def __call__(self, session_id: str, batch: list[Event]) -> runs.Outcome:
        if self._agent is None:
            self._agent = self._factory()
        session = self._agent.session
        # The row the worker started for this batch. `start` finds it again.
        run = runs.start(self._agent.conn, session_id, [e.id for e in batch])
        if session.session_id != session_id:
            session.switch(session_id)   # load that conversation's recent turns
        text = render(batch)
        self._log(f"{session_id} › {text}")
        # send_message reads this to key its rows to the run it is called from.
        with outbox.running(self._agent.settings.home, str(run.id), reply_address(batch)):
            result = self._agent.respond(text, observer=self._observer,
                                         source=batch[0].source)
        self._log(f"waku › {result.reply}")
        return runs.Outcome(result.reply, turn_id=(result.receipt or {}).get("turn_id", ""))

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
        run = runs.start(self.conn, first.session_id, ids, now=self._now())
        try:
            outcome = self._run(first.session_id, batch)
        except Exception as exc:
            from waku.gateway.runner import safe_exception

            runs.finish(self.conn, run.id, "failed", now=self._now())
            events.mark(self.conn, ids, "failed")
            self._log(f"(serve) the run for {first.session_id} failed: {safe_exception(exc)}")
        else:
            if not isinstance(outcome, runs.Outcome):
                outcome = runs.Outcome(outcome)
            self._queue_reply(run.id, batch, outcome.reply)
            runs.finish(self.conn, run.id, "done", outcome.turn_id, now=self._now())
            events.mark(self.conn, ids, "done")
        return True

    def _queue_reply(self, run_id: int, batch: list[Event], reply: str | None) -> None:
        """The run's reply becomes one outbox row. Its key is `<run id>:0`."""
        if is_silent(reply):
            return
        address = reply_address(batch)
        if address is None:
            self._log(f"(serve) the run for {batch[0].session_id} has nowhere to reply")
            return
        channel, target = address
        outbox.insert(self.conn, run_id=str(run_id), channel=channel, target=target,
                      body=reply.strip(), idempotency_key=f"{run_id}:0", now=self._now())

    def run_forever(self, idle: float = IDLE_SECONDS) -> None:
        recovered = self.recover()
        if recovered:
            self._log(f"(serve) {recovered} message(s) from a run that did not finish "
                      "will run again")
        while not self.stop.is_set():
            if not self.step():
                self.stop.wait(idle)
