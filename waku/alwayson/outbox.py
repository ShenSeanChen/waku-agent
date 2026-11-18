"""The outbox: one row in `outbox` per outgoing message (spec 023, decision 5).

A run never calls Telegram, Slack or GitHub. It calls `insert`, and the
dispatcher (dispatcher.py) sends the row. `idempotency_key` is UNIQUE, so a
run that is replayed after a crash writes the same key again and the second
row is dropped: the person gets one reply.

  pending   waiting for its next attempt (next_attempt_at)
  sent      delivered
  dead      failed 5 times; the dispatcher stops trying
  cancelled someone cancelled it before it was sent (waiting.py)

Read the table yourself:  sqlite3 ~/.waku/state.db 'SELECT * FROM outbox'
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

CHANNELS = ("telegram", "slack", "github")
# The wait after the first, second, third and fourth failure.
BACKOFF_SECONDS = (10, 60, 600, 3600)
MAX_ATTEMPTS = 5
_MAX_ERROR_CHARS = 300

# An address names one place a message can go, in the form a session id has:
#   telegram:<chat id>   slack:<channel>[:<thread>]   github:<owner>/<repo>#<number>
_TELEGRAM_RE = re.compile(r"^telegram:(-?\d+)$")
_SLACK_RE = re.compile(r"^slack:([A-Z0-9]+)(?::(\d+\.\d+))?$")
_GITHUB_RE = re.compile(r"^github:([^#\s]+)#([1-9]\d*)$")


def parse_address(text: str) -> tuple[str, dict] | None:
    """(channel, target) for an address, or None when the text is not one."""
    from waku.tools.github import _REPO_RE

    text = (text or "").strip()
    if found := _TELEGRAM_RE.match(text):
        return "telegram", {"chat_id": int(found[1])}
    if found := _SLACK_RE.match(text):
        target = {"channel": found[1]}
        if found[2]:
            target["thread_ts"] = found[2]
        return "slack", target
    if (found := _GITHUB_RE.match(text)) and _REPO_RE.match(found[1]):
        return "github", {"repo": found[1], "number": int(found[2])}
    return None


def format_address(channel: str, target: dict) -> str:
    """The address a (channel, target) pair was parsed from."""
    if channel == "telegram":
        return f"telegram:{target.get('chat_id')}"
    if channel == "slack":
        thread = target.get("thread_ts")
        return f"slack:{target.get('channel')}" + (f":{thread}" if thread else "")
    return f"github:{target.get('repo')}#{target.get('number')}"


def owner_addresses() -> list[tuple[str, dict]]:
    """WAKU_OWNER: where the person who runs this agent can be reached, as a
    comma-separated list of addresses. A value that is not an address is
    dropped."""
    found = (parse_address(value) for value in os.getenv("WAKU_OWNER", "").split(","))
    return [address for address in found if address]


def owner_name() -> str:
    """WAKU_OWNER_NAME: what other people read when the agent names the person
    who runs it. Empty when it is not set."""
    return os.getenv("WAKU_OWNER_NAME", "").strip()


@dataclass(frozen=True)
class Message:
    id: int
    run_id: str            # the run that wrote it
    channel: str           # telegram, slack or github
    target: dict           # a chat id, a channel and thread, or an issue
    body: str
    idempotency_key: str
    status: str
    attempts: int
    next_attempt_at: str   # UTC, ISO 8601
    last_error: str


def utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    """One fixed UTC form, so comparing two stamps as text compares the times."""
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _message(row: sqlite3.Row) -> Message:
    return Message(row["id"], row["run_id"], row["channel"], json.loads(row["target"]),
                   row["body"], row["idempotency_key"], row["status"], row["attempts"],
                   row["next_attempt_at"], row["last_error"])


def insert(conn: sqlite3.Connection, *, run_id: str, channel: str, target: dict, body: str,
           idempotency_key: str, now: datetime | None = None) -> int | None:
    """Write one pending row that is due at once. Returns its id, or None when
    a row with this idempotency_key exists already, sent or not."""
    if channel not in CHANNELS:
        raise ValueError(f"'{channel}' is not an outbox channel: {', '.join(CHANNELS)}")
    cursor = conn.execute(
        "INSERT OR IGNORE INTO outbox (run_id, channel, target, body, idempotency_key, "
        "next_attempt_at) VALUES (?, ?, ?, ?, ?, ?)",
        (str(run_id), channel, json.dumps(target), body, idempotency_key,
         _stamp(now or utc_now())))
    conn.commit()
    return cursor.lastrowid if cursor.rowcount else None


def get(conn: sqlite3.Connection, message_id: int) -> Message | None:
    row = conn.execute("SELECT * FROM outbox WHERE id = ?", (message_id,)).fetchone()
    return _message(row) if row else None


def due(conn: sqlite3.Connection, now: datetime) -> list[Message]:
    """Every pending row whose next attempt time has passed, oldest first."""
    rows = conn.execute(
        "SELECT * FROM outbox WHERE status = 'pending' AND next_attempt_at <= ? ORDER BY id",
        (_stamp(now),)).fetchall()
    return [_message(row) for row in rows]


def mark_sent(conn: sqlite3.Connection, message_id: int) -> None:
    conn.execute("UPDATE outbox SET status = 'sent', last_error = '' WHERE id = ?",
                 (message_id,))
    conn.commit()


def record_failure(conn: sqlite3.Connection, message_id: int, error: str,
                   now: datetime) -> str:
    """Count one failed delivery and return the row's status: `pending` with
    its next attempt time moved out, or `dead` after the fifth failure."""
    row = conn.execute("SELECT attempts FROM outbox WHERE id = ?", (message_id,)).fetchone()
    attempts = row["attempts"] + 1
    if attempts >= MAX_ATTEMPTS:
        status, next_attempt = "dead", _stamp(now)
    else:
        status = "pending"
        next_attempt = _stamp(now + timedelta(seconds=BACKOFF_SECONDS[attempts - 1]))
    conn.execute(
        "UPDATE outbox SET status = ?, attempts = ?, next_attempt_at = ?, last_error = ? "
        "WHERE id = ?", (status, attempts, next_attempt, error[:_MAX_ERROR_CHARS], message_id))
    conn.commit()
    return status


# ---- the run in flight -----------------------------------------------------

@dataclass
class RunContext:
    """What a tool needs to know about the run it is called from."""
    run_id: str
    address: tuple[str, dict] | None   # the conversation the run's events came from
    sends: int = 0                     # the messages this run has queued itself
    timers: int = 0                    # the timers this run has set
    # Set while the worker runs a call the owner approved (approvals.py): the
    # key of the one row that call may write.
    approved_key: str | None = None

    def next_key(self) -> str:
        """`<run>:1`, `<run>:2`, ... in call order. The run's reply is `<run>:0`.
        A replayed run makes the same calls in the same order, so it writes
        the same keys. An approved call writes its own key, however often it
        is run."""
        if self.approved_key:
            return self.approved_key
        self.sends += 1
        return f"{self.run_id}:{self.sends}"

    def next_timer_key(self) -> str:
        """`<run>:t1`, `<run>:t2`, ... in call order, for the same reason."""
        self.timers += 1
        return f"{self.run_id}:t{self.timers}"


# One worker per home runs one run at a time, so a home has at most one entry.
_RUNS: dict[Path, RunContext] = {}
_RUNS_LOCK = threading.Lock()


@contextmanager
def running(home: Path, run_id: str, address: tuple[str, dict] | None,
            approved_key: str | None = None):
    """Mark a run as in flight for this home while the block runs."""
    context = RunContext(run_id, address, approved_key=approved_key)
    with _RUNS_LOCK:
        _RUNS[Path(home)] = context
    try:
        yield context
    finally:
        with _RUNS_LOCK:
            _RUNS.pop(Path(home), None)


def current_run(home: Path) -> RunContext | None:
    with _RUNS_LOCK:
        return _RUNS.get(Path(home))
