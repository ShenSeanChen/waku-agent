"""The one worker: it claims events, batches them per session and starts one
run for each batch (spec 023, decision 2).

  claim the oldest pending row (a message, or a timer that came due)
  wait 3 seconds, so a second message sent right after the first joins it
  claim every pending row of that session
  take out each row that is the owner's yes or no to a call that waits, and
    act on it (approvals.py)
  one run with all of them, through waku.respond()
  write the run's reply into the outbox, unless the run chose to say nothing.
    A reply to a place other people read (a GitHub issue, a Slack channel)
    waits for the owner's yes like any call that writes to a third party
  close the run's row in `runs`: done, failed, or parked when it left a
    call waiting for the owner's yes. A run that failed sends one plain
    sentence in the reply's place. A parked run's reply is not sent: the
    request to the owner is the one message
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

import dataclasses
import os
import re
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from waku.alwayson import approvals, events, outbox, runs
from waku.alwayson.events import Event
from waku.memory import tool_note

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


def serving(home: Path) -> bool:
    """Whether a `waku serve` holds this home's lock right now. A tool asks,
    so that it only tells a person a message or a timer waits when it does."""
    lock = ServeLock(Path(home))
    try:
        handle = open(lock.path, "a+")  # noqa: SIM115  (closed below)
    except OSError:
        return False
    try:
        _lock(handle)
    except OSError:
        return True
    finally:
        handle.close()
    return False


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

[Nobody sent this message. A call from this conversation waited for \
approval, and the line above says what happened to it. Your reply is \
sent to this conversation. If it does not need to hear the outcome, reply \
with the single word SILENT and nothing is sent.]"""

# What a run reads when a timer woke it. Nobody is waiting for an answer, so
# the model is told that it may end the run without sending one.
TIMER_PROMPT = """\
[A timer you set on {set_at} is due now. Nobody sent this \
message. Your note to yourself was:]

{note}

[Do what the note asks. Your reply is sent to the conversation that set the \
timer. If there is nothing worth sending, reply with the single word SILENT \
and nothing is sent.]"""


# What a run reads when an event started it and no person asked for anything,
# for example a new issue with the label. The last sentence says what happens
# to a reply: it goes out, or the owner reads it first.
UNPROMPTED_PROMPT = """\
{text}

[Nobody asked you for anything: this arrived by itself. If you have nothing \
useful to add, reply with the single word SILENT. That is the right answer \
most of the time, and then nothing is posted. {reply}]"""
UNPROMPTED_ASKS = ("If you do reply, the owner reads it first and decides whether it "
                   "is posted.")
UNPROMPTED_POSTS = "If you do reply, your reply is posted where this came from."
# The one line the owner gets about such an event, after the words the source
# gave it: "New issue acme/app#12: 'Login fails on Safari'. I did nothing."
DID_NOTHING = "I did nothing."
WANTS_TO_COMMENT = "I want to comment, see the request above."
COMMENTED = "I commented."


# What a run reads last when its reply goes to the owner's own chat. Without
# it the model told the owner that "the owner" had been asked, and to "check
# Telegram", in the owner's Telegram chat. It names no one and no address, so
# it holds whether or not WAKU_OWNER_NAME is set.
OWNER_READS = ("[You are talking to the person who runs this assistant. They are the one "
               "who approves its calls, and they answer yes or no in this chat. Address "
               "them as \"you\", never in the third person, and never show them a chat id "
               "or an address.]")


def to_owner(batch: list[Event]) -> bool:
    """Whether the owner is the person a run talks to: its reply goes to the
    owner's own chat, and every message in the batch came from that chat's
    one person. In a private Telegram chat the chat's id is the person's id."""
    address = reply_address(batch)
    if not approvals.is_owner_chat(address):
        return False
    owner = str(address[1].get("chat_id"))
    return all(str((e.payload.get("sender") or {}).get("id", "")) == owner
               for e in batch if e.source not in INTERNAL)


_PLACE = r"(?:telegram:|chat\s+id\s*:?\s*|chat\s+)"


def without_chat_ids(reply: str, address: tuple[str, dict] | None) -> str:
    """A reply with no chat id of the owner in it. The model reads such an id
    in an address and can repeat it; no person needs to read one."""
    for channel, target in outbox.owner_addresses():
        chat_id = str(target.get("chat_id", ""))
        if channel != "telegram" or not chat_id.lstrip("-").isdigit():
            continue
        words = "this chat" if (channel, target) == address else "the owner's chat"
        # A long number is an id wherever it stands. A short one is replaced
        # only where the words before it say it is a chat.
        bare = "?" if len(chat_id) >= 6 else ""
        reply = re.sub(rf"{_PLACE}{bare}(?<!\d){re.escape(chat_id)}(?!\d)", words, reply,
                       flags=re.IGNORECASE)
    return reply


# What a run reads after the owner's bare yes or no when no call waits. On
# 2026-11-17 the owner sent "Approve" with nothing waiting, and the model
# invented a request it said was on Telegram.
NOTHING_WAITS = ("[Nothing is waiting for approval: no call waits for a yes or a no. If "
                 "this message answers a question you asked, take it as that answer. If "
                 "it is meant to approve or decline a call, say plainly that nothing is "
                 "waiting for approval.]")


# What a person reads when a run raised. The traceback and the reason are in
# the log of `waku serve`, never in a message: a reason can quote an address
# or a key. On 2026-11-17 a run failed after the owner approved a call, and
# the owner heard nothing.
FAILED = ("Something went wrong on my side and I could not finish that. Please send it "
          "again. If it happens again, restart Waku on the computer it runs on.")
FAILED_AFTER = ("Something went wrong on my side after that, so I cannot say more about "
                "it. Ask me again if you need more.")
NOT_ANSWERED = ("Something went wrong on my side and I could not answer in {where}. "
                "Nothing was posted there.")
# What the conversation reads as the result of an approved call that raised.
CALL_FAILED = "Error: the call did not finish. Something went wrong on my side."


def _text(event: Event) -> str:
    text = event.payload.get("text", "")
    if event.payload.get("nothing_waits"):
        return f"{text}\n\n{NOTHING_WAITS}"
    if event.source == approvals.SOURCE:
        return APPROVAL_PROMPT.format(outcome=text)
    if event.payload.get("unprompted"):
        address = reply_address([event])
        free = address is not None and approvals.reply_is_free(address)
        return UNPROMPTED_PROMPT.format(
            text=text, reply=UNPROMPTED_POSTS if free else UNPROMPTED_ASKS)
    if event.source != "timer":
        return text
    return TIMER_PROMPT.format(set_at=event.payload.get("set_at", "an earlier day"), note=text)


def render(batch: list[Event]) -> str:
    """The one user message a run starts with: each event's text, oldest first."""
    text = "\n\n".join(_text(e) for e in batch).strip()
    return f"{text}\n\n{OWNER_READS}" if to_owner(batch) else text


class AgentRun:
    """One run: a batch of events becomes one waku.respond() turn in the
    session the events name. The agent is built on first use, on the worker's
    thread, because Waku owns a SQLite connection that stays on one thread."""

    def __init__(self, agent_factory: Callable[[], Any], observer=None,
                 log: Callable[[str], None] = print) -> None:
        self._factory, self._observer, self._log = agent_factory, observer, log
        self._agent: Any | None = None
        self._connectors: frozenset[str] | None = None

    def _build(self) -> None:
        """Build the agent on first use, and build it again when the servers
        that connect without a browser have changed (spec 025): the owner
        signed in to one, or added or removed one."""
        from waku.tools import connectors

        if self._agent is not None:
            if connectors.ready(self._agent.settings.home) == self._connectors:
                return
            self._log("(serve) the connected servers changed; loading their tools")
            self.close()
        self._agent = self._factory()
        self._connectors = connectors.ready(self._agent.settings.home)

    def __call__(self, session_id: str, batch: list[Event]) -> runs.Outcome:
        self._build()
        session = self._agent.session
        # The row the worker started for this batch.
        run = (runs.find(self._agent.conn, session_id, batch[0].id)
               or runs.start(self._agent.conn, session_id, [e.id for e in batch]))
        if session.session_id != session_id:
            session.switch(session_id)   # load that conversation's recent turns
        text = render(batch)
        self._log(f"{session_id} › {text}")
        # send_message reads this to key its rows to the run it is called from.
        with outbox.running(self._agent.settings.home, str(run.id), reply_address(batch)):
            result = self._agent.respond(text, observer=self._observer,
                                         source=batch[0].source)
        self._log(f"waku › {result.reply}")
        from waku.tools import connectors

        reply = connectors.without_dead_links(self._agent.settings.home, result.reply)
        if reply != result.reply:
            self._log("(serve) took a sign-in link that nothing waits for out of the reply")
        return runs.Outcome(reply, turn_id=(result.receipt or {}).get("turn_id", ""))

    def execute(self, run: runs.Run, address: tuple[str, dict] | None) -> str:
        """Run the call a parked run stored, exactly as stored, and return
        what the tool answered. The model is not asked. The row the call
        writes carries the run's id and the call's hash as its key, so
        running it twice sends one message."""
        self._build()
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
        self._remind_and_expire()
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
            self._queue_failed(run.id, batch)
        else:
            if not isinstance(outcome, runs.Outcome):
                outcome = runs.Outcome(outcome)
            status = self._ended(run.id, "done")
            if status == "parked":
                self._queue_asked(run.id, batch)
            else:
                self._queue_reply(run.id, batch, outcome.reply)
                status = self._ended(run.id, "done")   # the reply itself may wait
            self._tell_owner(run.id, batch, status)
            runs.finish(self.conn, run.id, status, outcome.turn_id, now=self._now())
            events.mark(self.conn, ids, "done")
        return True

    def _remind_and_expire(self) -> None:
        """Say once that a call still waits, and say once that it stopped
        waiting (spec 024, decision 8). At 48 hours the owner gets a reminder
        where the request went. At 72 hours the owner reads that nothing was
        posted, and the conversation that asked gets the outcome as an event.
        Every row has its own key, so a step that is repeated says nothing
        twice."""
        now = self._now()
        for run in runs.to_remind(self.conn, now):
            if runs.overdue(run, now):
                continue   # it expires in this step; the expiry says enough
            self._tell_asked(run, approvals.REMINDER, f"remind:{run.id}")
            runs.mark_reminded(self.conn, run.id)
        for run in runs.expire(self.conn, now):
            self._tell_asked(run, approvals.STOPPED, f"expired:{run.id}")
            asked_in = [e for e in (events.get(self.conn, i) for i in run.event_ids) if e]
            address = reply_address(asked_in)
            # The owner's own chat has read it, and a reply that waited has no
            # conversation to tell.
            if (address == approvals.asked_address(run)
                    or (run.pending_call or {}).get("reply")):
                continue
            payload: dict = {"text": approvals.said(run, approvals.EXPIRED)}
            if address:
                payload["channel"], payload["reply_to"] = address
            events.insert(self.conn, source=approvals.SOURCE,
                          external_id=f"approval:{run.id}", session_id=run.session_id,
                          payload=payload, received_at=outbox._stamp(now))

    def _tell_asked(self, run: runs.Run, words: str, key: str) -> None:
        asked = approvals.asked_address(run)
        if asked is not None:
            outbox.insert(self.conn, run_id=str(run.id), channel=asked[0], target=asked[1],
                          body=approvals.said(run, words), idempotency_key=key,
                          now=self._now())

    def _read_answers(self, batch: list[Event]) -> list[Event]:
        """Take the owner's answers out of a batch and act on each (spec 023,
        decision 4). Returns the events that are ordinary messages."""
        ordinary = []
        for event in batch:
            answer = (None if event.source in INTERNAL
                      else approvals.read_answer(event.payload.get("text", "")))
            waiting = approvals.waiting_for(self.conn, event, self._now()) if answer else []
            nothing_waits = bool(answer) and not waiting and to_owner([event])
            if answer and answer[1]:
                waiting = [run for run in waiting if run.pending_hash.startswith(answer[1])]
            if not waiting or (answer[0] and self._execute is None):
                # The run reads that the owner's yes or no has nothing to
                # answer, so the model does not make up a call that it met.
                ordinary.append(dataclasses.replace(
                    event, payload={**event.payload, "nothing_waits": True})
                    if nothing_waits else event)
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
        output, raised = "", False
        if approved and approvals.intact(run):
            try:
                output = self._execute(run, address)
            except Exception as exc:
                from waku.gateway.runner import safe_exception

                # The reason stays in the log: it may quote an address or a key.
                self._log(f"(serve) the approved call of run {run.id} failed: "
                          f"{safe_exception(exc)}")
                output, raised = CALL_FAILED, True
        # A reply that waited is the end of its run: once it is posted or
        # dropped, the conversation has nothing to hear about the approval.
        if not (run.pending_call or {}).get("reply"):
            payload: dict = {"text": approvals.outcome(run, approved, output)}
            if address:
                payload["channel"], payload["reply_to"] = address
            events.insert(self.conn, source=approvals.SOURCE,
                          external_id=f"approval:{run.id}", session_id=run.session_id,
                          payload=payload, received_at=outbox._stamp(self._now()))
        ran = approved and approvals.intact(run) and not raised
        runs.resolve(self.conn, run.id, "failed" if approved and not ran else "done",
                     approved=ran, now=self._now())

    def _ended(self, run_id: int, status: str) -> str:
        """`parked` when the run left a call waiting for the owner's yes."""
        return "parked" if runs.get(self.conn, run_id).pending_hash else status

    def _tell_owner(self, run_id: int, batch: list[Event], status: str) -> None:
        """One line to the owner for an event no person sent (spec 024,
        decision 5): what arrived, and what the agent did about it. It goes
        to the owner's own address, never to the place the event came from."""
        arrived = [e.payload["unprompted"] for e in batch if e.payload.get("unprompted")]
        address = reply_address(batch)
        owner = next((a for a in outbox.owner_addresses() if a != address), None)
        if not arrived or owner is None:
            return
        replied = self.conn.execute("SELECT 1 FROM outbox WHERE idempotency_key = ?",
                                    (f"{run_id}:0",)).fetchone()
        did = (WANTS_TO_COMMENT if status == "parked" else COMMENTED if replied
               else DID_NOTHING)
        outbox.insert(self.conn, run_id=str(run_id), channel=owner[0], target=owner[1],
                      body="\n".join(f"{what}. {did}" for what in arrived),
                      idempotency_key=f"{run_id}:note", now=self._now())

    def _queue_failed(self, run_id: int, batch: list[Event]) -> None:
        """A run that raised still says one plain sentence: what did not work
        and what to do. The reason stays in the log. When the batch held the
        outcome of a call the owner approved, the sentence follows it, so the
        person reads what the call answered. A place other people read gets
        nothing, and the owner is told there was no answer there."""
        address = reply_address(batch)
        if address is None:
            return
        outcomes = [e.payload.get("text", "") for e in batch if e.source == approvals.SOURCE]
        body = "\n\n".join([*outcomes, FAILED_AFTER if outcomes else FAILED])
        if not approvals.reply_is_free(address):
            owner = next(iter(outbox.owner_addresses()), None)
            if owner is None:
                return
            channel, target = address
            where = (f"{target.get('repo')}#{target.get('number')}" if channel == "github"
                     else channel.capitalize())
            address, body = owner, NOT_ANSWERED.format(where=where)
        outbox.insert(self.conn, run_id=str(run_id), channel=address[0], target=address[1],
                      body=without_chat_ids(body, address),
                      idempotency_key=f"{run_id}:failed", now=self._now())

    def _queue_asked(self, run_id: int, batch: list[Event]) -> None:
        """A parked run sends one message, the request (spec 024, decision 1).
        Whatever the model replied is dropped. When the owner was asked
        somewhere else, the chat that asked reads one fixed line in the
        reply's place, under the reply's key. A GitHub issue reads nothing."""
        address = reply_address(batch)
        asked = (runs.get(self.conn, run_id).pending_call or {}).get("asked") or {}
        if address is None or address == (asked.get("channel"), asked.get("target")):
            return
        if address[0] == "github":
            return   # a comment is under the owner's name, so nothing is posted unasked
        channel, target = address
        outbox.insert(self.conn, run_id=str(run_id), channel=channel, target=target,
                      body=approvals.asked_line(), idempotency_key=f"{run_id}:0",
                      now=self._now())

    def _queue_reply(self, run_id: int, batch: list[Event], reply: str | None) -> None:
        """The run's reply becomes one outbox row. Its key is `<run id>:0`."""
        reply = tool_note.strip(reply or "")   # the note of history is not for a person
        if is_silent(reply):
            return
        address = reply_address(batch)
        if address is None:
            self._log(f"(serve) the run for {batch[0].session_id} has nowhere to reply")
            return
        channel, target = address
        reply = without_chat_ids(reply, address)
        if not approvals.reply_is_free(address):
            self._park_reply(run_id, batch[0].session_id, address, reply.strip())
            return
        outbox.insert(self.conn, run_id=str(run_id), channel=channel, target=target,
                      body=reply.strip(), idempotency_key=f"{run_id}:0", now=self._now())

    def _park_reply(self, run_id: int, session_id: str, address: tuple[str, dict],
                    reply: str) -> None:
        """A reply that other people would read is a write (spec 024, decision
        5): it is stored as a send_message call, and the owner reads the exact
        text in the request. Nothing is queued for the conversation."""
        said = approvals.park(
            self.conn, run_id, "send_message",
            {"to": outbox.format_address(*address), "body": reply}, self._now(), reply=True)
        if said != approvals.WAITS:
            self._log(f"(serve) the reply for {session_id} was not sent. {said}")

    def run_forever(self, idle: float = IDLE_SECONDS) -> None:
        recovered = self.recover()
        if recovered:
            self._log(f"(serve) {recovered} message(s) from a run that did not finish "
                      "will run again")
        while not self.stop.is_set():
            if not self.step():
                self.stop.wait(idle)
