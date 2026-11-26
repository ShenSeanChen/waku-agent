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
    receipt: str = ""      # what the channel answered: a Slack message's ts


def utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    """One fixed UTC form, so comparing two stamps as text compares the times."""
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _message(row: sqlite3.Row) -> Message:
    return Message(row["id"], row["run_id"], row["channel"], json.loads(row["target"]),
                   row["body"], row["idempotency_key"], row["status"], row["attempts"],
                   row["next_attempt_at"], row["last_error"], row["receipt"])


def insert(conn: sqlite3.Connection, *, run_id: str, channel: str, target: dict, body: str,
           idempotency_key: str, now: datetime | None = None) -> int | None:
    """Write one pending row that is due at once. Returns its id, or None when
    a row with this idempotency_key exists already, sent or not, and when the
    body is empty."""
    if channel not in CHANNELS:
        raise ValueError(f"'{channel}' is not an outbox channel: {', '.join(CHANNELS)}")
    # No message a person reads carries the "[tools used: ...]" note of
    # history, whoever wrote it. A body that is only a note is not a message.
    from waku.memory import tool_note

    body = tool_note.strip(body)
    if not body:
        return None
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


def mark_sent(conn: sqlite3.Connection, message_id: int, receipt: str = "") -> None:
    """Mark a row delivered, and keep what the channel answered (spec 035):
    the ts of a Slack message, so a drafted review can wait in a thread under
    that morning's digest."""
    conn.execute("UPDATE outbox SET status = 'sent', last_error = '', receipt = ? WHERE id = ?",
                 (receipt, message_id))
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


# ---- a request in a thread under its digest (spec 035) ----------------------

def digest_thread(conn: sqlite3.Connection, channel: str) -> str:
    """The Slack ts of the newest digest sent to this channel, or ""."""
    row = conn.execute(
        "SELECT receipt FROM outbox WHERE channel = 'slack' AND status = 'sent' "
        "AND receipt != '' AND json_extract(target, '$.digest') = 1 "
        "AND json_extract(target, '$.channel') = ? ORDER BY id DESC LIMIT 1",
        (channel,)).fetchone()
    return row["receipt"] if row else ""


def resolve_target(conn: sqlite3.Connection, message: Message) -> dict | None:
    """Where a row goes now. A row marked `under_digest` goes into the thread
    of its run's digest once that digest is sent, or else under the newest
    digest in the channel, or else into the channel itself. None means the
    run's own digest is still pending: the request is written first, while
    the run is in flight, and waits one step so the digest is posted above it."""
    target = message.target
    if not target.get("under_digest"):
        return target
    own = conn.execute(
        "SELECT status, receipt FROM outbox WHERE idempotency_key = ? "
        "AND json_extract(target, '$.digest') = 1", (f"{message.run_id}:0",)).fetchone()
    if own is not None and own["status"] == "pending":
        return None
    thread = (own["receipt"] if own is not None and own["status"] == "sent" and own["receipt"]
              else digest_thread(conn, str(target.get("channel", ""))))
    resolved = {key: value for key, value in target.items() if key != "under_digest"}
    if thread:
        resolved["thread_ts"] = thread
    return resolved


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
    # Set for the PR desk's run (spec 035): the only tools it may call. A pull
    # request's text reaches that run, and anyone can write one.
    only: frozenset[str] | None = None

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
            approved_key: str | None = None, only: frozenset[str] | None = None):
    """Mark a run as in flight for this home while the block runs."""
    context = RunContext(run_id, address, approved_key=approved_key, only=only)
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
