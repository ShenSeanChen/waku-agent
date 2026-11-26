"""The dispatcher: it sends each row of the outbox (spec 023, decision 5).

  take every pending row whose next attempt time has passed
  hand it to the sender for its channel
  mark it sent, or count the failure

A failed row waits 10 seconds, then 1 minute, 10 minutes and 1 hour. The fifth
failure marks it `dead`, and the owner gets one notice on another channel when
WAKU_OWNER names one.

A send that reached the channel just before the process died is sent again at
the next start, because the row was not marked `sent` yet. v1 accepts that for
chat messages.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from waku.alwayson import outbox
from waku.alwayson.outbox import Message

IDLE_SECONDS = 1.0
NOTICE_PREFIX = "dead:"
_NOTICE_BODY_CHARS = 200

# A sender delivers one message and returns, or raises when it could not. It
# may return what the channel answered, a Slack message's ts, and the row
# keeps it (spec 035).
Sender = Callable[[Message], "str | None"]


class Dispatcher:
    def __init__(self, conn: sqlite3.Connection, senders: Mapping[str, Sender], *,
                 now: Callable[[], datetime] = outbox.utc_now,
                 owner: Sequence[tuple[str, dict]] = (),
                 log: Callable[[str], None] = print) -> None:
        self.conn, self._senders, self._now, self._log = conn, dict(senders), now, log
        self._owner = list(owner)
        self.stop = threading.Event()

    def step(self) -> int:
        """Try every due row once. Returns how many rows were tried. A row
        that waits for its digest is left for the next step (spec 035)."""
        rows = outbox.due(self.conn, self._now())
        for message in rows:
            target = outbox.resolve_target(self.conn, message)
            if target is None:
                continue
            self._deliver(dataclasses.replace(message, target=target))
        return len(rows)

    def _deliver(self, message: Message) -> None:
        try:
            sender = self._senders.get(message.channel)
            if sender is None:
                raise RuntimeError(f"no sender is configured for {message.channel}")
            receipt = sender(message)
        except Exception as exc:
            from waku.gateway.runner import safe_exception

            error = safe_exception(exc)
            status = outbox.record_failure(self.conn, message.id, error, self._now())
            if status == "dead":
                self._log(f"(serve) gave up on a {message.channel} message after "
                          f"{outbox.MAX_ATTEMPTS} attempts: {error}")
                self._notify_owner(message, error)
        else:
            outbox.mark_sent(self.conn, message.id, receipt if isinstance(receipt, str) else "")

    def _notify_owner(self, dead: Message, error: str) -> None:
        """One notice on a channel other than the one that failed. A notice
        that dies itself gets no notice of its own."""
        if dead.idempotency_key.startswith(NOTICE_PREFIX):
            return
        for channel, target in self._owner:
            if channel != dead.channel and channel in self._senders:
                outbox.insert(
                    self.conn, run_id=dead.run_id, channel=channel, target=target,
                    body=(f"I could not deliver a message on {dead.channel} after "
                          f"{outbox.MAX_ATTEMPTS} attempts ({error}). It starts: "
                          f'"{dead.body[:_NOTICE_BODY_CHARS]}"'),
                    idempotency_key=f"{NOTICE_PREFIX}{dead.id}", now=self._now())
                return

    def run_forever(self, idle: float = IDLE_SECONDS) -> None:
        while not self.stop.is_set():
            try:
                self.step()
            except Exception as exc:
                self._log(f"(serve) the dispatcher hit an error: {type(exc).__name__}")
            self.stop.wait(idle)
