"""The one worker: it claims events, batches them per session and starts one
run for each batch (spec 023, decision 2).

  claim the oldest pending row (a message, or a timer that came due)
  wait 3 seconds, so a second message sent right after the first joins it
  claim every pending row of that session
  take out each row that is the owner's yes or no to a call that waits, and
    act on it (approvals.py)
  one run with all of them, through waku.respond()
  write the run's reply into the outbox, unless the run chose to say nothing
  close the run's row in `runs`: done, failed, or parked when it left a
    call waiting for the owner's yes
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

from waku.alwayson import approvals, events, outbox, runs
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
    and the outcome of an approval come in on no channel, so each names the
    channel of the conversation it belongs to."""
    for event in reversed(batch):
        target = event.payload.get("reply_to")
        channel = event.payload.get("channel") if event.source in INTERNAL else event.source
        if target and channel in outbox.CHANNELS:
            return channel, target
    return None


# The sources that `waku serve` writes itself. Nobody sent these events.
INTERNAL = ("timer", approvals.SOURCE)

# What a run reads when the owner answered a call that was waiting. The first
# line is the outcome.
APPROVAL_PROMPT = """\
[{outcome}]

[Nobody sent this message. A call from this conversation waited for the \
owner's approval, and the line above says what happened to it. Your reply is \
sent to this conversation. If it does not need to hear the outcome, reply \
with the single word SILENT and nothing is sent.]"""

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
    if event.source == approvals.SOURCE:
        return APPROVAL_PROMPT.format(outcome=text)
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

    def execute(self, run: runs.Run, address: tuple[str, dict] | None) -> str:
        """Run the call a parked run stored, exactly as stored, and return
        what the tool answered. The model is not asked. The row the call
        writes carries the run's id and the call's hash as its key, so
        running it twice sends one message."""
        if self._agent is None:
            self._agent = self._factory()
        call = run.pending_call
        self._log(f"(serve) the owner approved {call['tool']} for {run.session_id}")
        with outbox.running(self._agent.settings.home, str(run.id), address,
                            approved_key=f"{run.id}:{run.pending_hash}"):
            return self._agent.tools.execute(call["tool"], call["args"])

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
                 run: Callable[[str, list[Event]], str | runs.Outcome | None], *,
                 execute: Callable[[runs.Run, tuple[str, dict] | None], str] | None = None,
                 batch_wait: float = BATCH_WAIT_SECONDS,
                 wait: Callable[[float], Any] | None = None,
                 now: Callable[[], datetime] = outbox.utc_now,
                 log: Callable[[str], None] = print) -> None:
        self.conn, self._run, self.batch_wait, self._log = conn, run, batch_wait, log
        self._now = now
        # What runs a call the owner approved. AgentRun brings its own.
        self._execute = execute or getattr(run, "execute", None)
        self.stop = threading.Event()
        # stop.wait returns early when `waku serve` is told to stop. The evals
        # pass their own wait, so no test sleeps for real seconds.
        self._wait = wait or self.stop.wait

    def recover(self) -> int:
        return events.recover(self.conn)

    def step(self) -> bool:
        """Run one batch. False when there was nothing to run."""
        runs.expire(self.conn, self._now())
        first = events.claim_next(self.conn)
        if first is None:
            return False
        self._wait(self.batch_wait)
        batch = [first, *events.claim_session(self.conn, first.session_id)]
        ids = [e.id for e in batch]
        if self.stop.is_set():
            events.mark(self.conn, ids, "pending")   # the next start runs them
            return False
        batch = self._read_answers(batch)
        if not batch:
            return True   # every event was the owner's answer to a waiting call
        ids = [e.id for e in batch]
        run = runs.start(self.conn, first.session_id, ids, now=self._now())
        try:
            outcome = self._run(first.session_id, batch)
        except Exception as exc:
            from waku.gateway.runner import safe_exception

            runs.finish(self.conn, run.id, self._ended(run.id, "failed"), now=self._now())
            events.mark(self.conn, ids, "failed")
            self._log(f"(serve) the run for {first.session_id} failed: {safe_exception(exc)}")
        else:
            if not isinstance(outcome, runs.Outcome):
                outcome = runs.Outcome(outcome)
            self._queue_reply(run.id, batch, outcome.reply)
            runs.finish(self.conn, run.id, self._ended(run.id, "done"), outcome.turn_id,
                        now=self._now())
            events.mark(self.conn, ids, "done")
        return True

    def _read_answers(self, batch: list[Event]) -> list[Event]:
        """Take the owner's answers out of a batch and act on each (spec 023,
        decision 4). Returns the events that are ordinary messages."""
        ordinary = []
        for event in batch:
            answer = (None if event.source in INTERNAL
                      else approvals.read_answer(event.payload.get("text", "")))
            waiting = approvals.waiting_for(self.conn, event, self._now()) if answer else []
            if answer and answer[1]:
                waiting = [run for run in waiting if run.pending_hash.startswith(answer[1])]
            if not waiting or (answer[0] and self._execute is None):
                ordinary.append(event)
                continue
            if len(waiting) > 1:
                channel, target = event.source, event.payload["reply_to"]
                outbox.insert(self.conn, run_id=str(waiting[0].id), channel=channel,
                              target=target, body=approvals.which(waiting),
                              idempotency_key=f"which:{event.id}", now=self._now())
            else:
                self._resolve(waiting[0], answer[0])
            events.mark(self.conn, [event.id], "done")
        return ordinary

    def _resolve(self, run: runs.Run, approved: bool) -> None:
        """Run the stored call on a yes, discard it on a no, and write the
        outcome as an event in the conversation that parked. Each step can
        happen twice without a second effect: the call's row has one key, the
        event has one external id, and a closed run is no longer parked."""
        parked_events = [e for e in (events.get(self.conn, i) for i in run.event_ids) if e]
        address = reply_address(parked_events)
        output = ""
        if approved and approvals.intact(run):
            output = self._execute(run, address)
        payload: dict = {"text": approvals.outcome(run, approved, output)}
        if address:
            payload["channel"], payload["reply_to"] = address
        events.insert(self.conn, source=approvals.SOURCE, external_id=f"approval:{run.id}",
                      session_id=run.session_id, payload=payload,
                      received_at=outbox._stamp(self._now()))
        runs.resolve(self.conn, run.id,
                     "failed" if approved and not approvals.intact(run) else "done")

    def _ended(self, run_id: int, status: str) -> str:
        """`parked` when the run left a call waiting for the owner's yes."""
        return "parked" if runs.get(self.conn, run_id).pending_hash else status

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
