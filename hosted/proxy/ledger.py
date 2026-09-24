"""ledger.db -- spend and platform-call times. The proxy's alone.

On the VM, ledger/ is owned by UID 10003 with mode 0700. The gateway never
opens this file: it reads spend over run/proxy/proxy.sock (internal.py), and
when that cannot answer it applies free's turn limit and /account says "Spend
is unavailable right now."

RESERVATIONS ARE AN AGGREGATE, not a row each, because that is what makes a
crash recoverable. The spec's spend table has one settled and one reserved
column per tenant per month; when the proxy starts, whatever is still reserved
is settled at its full amount, since the upstream call it was holding may
already have been billed. Per-reservation rows would have to be matched back
to calls that no longer exist.

Because reservations are an aggregate they are also FUNGIBLE: there is no
per-call identity to preserve, so releasing "0.40 of this tenant's outstanding
reservations" is well defined even when the month has turned over since the
reserve. That is what settle() and release() do, and it is what keeps a call in
flight at 00:00 UTC on the first from being billed twice -- see _release_locked.
"""

from __future__ import annotations

import math
import sqlite3
import threading
from pathlib import Path

PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    # FULL, not NORMAL: the transaction NORMAL can lose here is settled spend.
    "PRAGMA synchronous=FULL",
    "PRAGMA busy_timeout=5000",
)
# No foreign_keys pragma: neither table declares a foreign key, so turning it
# on would enforce nothing while implying a constraint that is not there.

SCHEMA = """
CREATE TABLE IF NOT EXISTS spend (
  tenant_id  TEXT NOT NULL,
  month      TEXT NOT NULL,
  settled    REAL NOT NULL DEFAULT 0,
  reserved   REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (tenant_id, month)
);
CREATE TABLE IF NOT EXISTS platform_call (
  tenant_id  TEXT PRIMARY KEY,
  at         REAL NOT NULL
);
"""


def _dollars(value: float, name: str) -> float:
    """Clamp at zero and refuse what arithmetic cannot recover from.

    The subtracting paths were clamped from the start and the adding paths were
    not, which made the file read as guarded while a negative actual raised the
    tenant's effective cap by its own size. Infinity is refused because it
    poisons every later comparison and because json.dumps writes it as bare
    `Infinity`, which is not JSON and would break the socket answer. NaN is
    refused here rather than, as before, by SQLite storing it as NULL and the
    NOT NULL constraint catching it -- a right answer resting on a coincidence.
    NaN is the dangerous one: every `settled >= cap` against it is False, so the
    cap would never trip.
    """
    amount = float(value)
    if not math.isfinite(amount):
        raise ValueError(f"{name} must be a finite number of dollars, not {value!r}")
    return max(0.0, amount)


class Ledger:
    """The proxy's spend book, behind one lock, for the reason ControlDb has
    one: the connection is opened check_same_thread=False, so the moment any
    call moves to an executor two threads share a connection sqlite3 does not
    serialise at the transaction level.

    Measured before the lock went in, eight threads each reserving a dollar,
    twenty rounds: 12 of 20 rounds ended with the wrong total. The failure is
    worse here than in ControlDb, which at least raised -- a ledger that loses
    a dollar silently shows an operator a tenant spending past the cap with
    nothing anywhere saying why.

    Every public method holds the lock across its statements and its commit.
    The _locked helpers hold the SQL that one public method needs from another,
    so nothing re-enters a public method and a plain Lock stays sufficient.
    """

    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        with self._lock:
            for pragma in PRAGMAS:
                self._conn.execute(pragma)
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def journal_mode(self) -> str:
        with self._lock:
            return self._conn.execute("PRAGMA journal_mode").fetchone()[0].lower()

    def synchronous(self) -> int:
        """The value in effect, not the text that was sent. A typo such as
        `synchronous=FUL` does not error: SQLite lands on NORMAL while the
        source still reads as a durability declaration, and only a readback
        sees the difference between declared and applied."""
        with self._lock:
            return int(self._conn.execute("PRAGMA synchronous").fetchone()[0])

    def _row_locked(self, tenant_id: str, month: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO spend (tenant_id, month) VALUES (?, ?)",
            (tenant_id, month))

    def _release_locked(self, tenant_id: str, month: str, dollars: float) -> None:
        """Release `dollars` of this tenant's outstanding reservations, taking
        `month` first and then spilling into any other month that still holds
        some, oldest first.

        The spill is what stops one call being billed twice. A call reserved at
        23:59 on the last of the month and settled at 00:00 on the first lands
        on two different UTC months; releasing only from the settle month would
        strand the reservation in the old one, where settle_leftovers would
        later charge it again at its full amount. Reservations are an aggregate
        with no per-call identity, so releasing the tenant's outstanding
        dollars wherever they sit is not an approximation -- it is the only
        thing the aggregate can mean.
        """
        remaining = dollars
        if remaining <= 0:
            return
        # `month <> ?` is 0 for the named month and 1 for the rest, so the named
        # month sorts first and the others follow oldest first.
        rows = self._conn.execute(
            "SELECT month, reserved FROM spend WHERE tenant_id = ? AND reserved > 0 "
            "ORDER BY (month <> ?), month", (tenant_id, month)).fetchall()
        for held_month, held in rows:
            if remaining <= 0:
                break
            take = min(held, remaining)
            self._conn.execute(
                "UPDATE spend SET reserved = max(0, reserved - ?) "
                "WHERE tenant_id = ? AND month = ?", (take, tenant_id, held_month))
            remaining -= take

    def spend(self, tenant_id: str, month: str) -> tuple[float, float]:
        with self._lock:
            row = self._conn.execute(
                "SELECT settled, reserved FROM spend WHERE tenant_id = ? AND month = ?",
                (tenant_id, month)).fetchone()
        return (row[0], row[1]) if row else (0.0, 0.0)

    def reserve(self, tenant_id: str, month: str, dollars: float) -> None:
        amount = _dollars(dollars, "dollars")
        with self._lock:
            self._row_locked(tenant_id, month)
            self._conn.execute(
                "UPDATE spend SET reserved = reserved + ? WHERE tenant_id = ? AND month = ?",
                (amount, tenant_id, month))
            self._conn.commit()

    def settle(self, tenant_id: str, month: str, *, reserved: float, actual: float) -> None:
        """Release `reserved` and charge `actual`, in one transaction, so the
        two can never be seen apart by the /account read on the other socket.

        `month` is the month the charge lands in. The release is not confined
        to it: see _release_locked for why a reservation that outlived its month
        must still come off.
        """
        held = _dollars(reserved, "reserved")
        charge = _dollars(actual, "actual")
        with self._lock:
            self._row_locked(tenant_id, month)
            self._release_locked(tenant_id, month, held)
            self._conn.execute(
                "UPDATE spend SET settled = settled + ? WHERE tenant_id = ? AND month = ?",
                (charge, tenant_id, month))
            self._conn.commit()

    def release(self, tenant_id: str, month: str, dollars: float) -> None:
        amount = _dollars(dollars, "dollars")
        with self._lock:
            self._row_locked(tenant_id, month)
            self._release_locked(tenant_id, month, amount)
            self._conn.commit()

    def settle_leftovers(self) -> float:
        """Called once when the proxy starts. Returns the dollars moved.

        The read and the move are one transaction -- BEGIN IMMEDIATE takes the
        write lock before the SUM -- so the number returned is the number moved
        even if something else is writing. It is called before the proxy serves,
        but a docstring saying "nothing else is running yet" is not a guarantee,
        and this is the one call that turns reservations into charges.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            total = self._conn.execute(
                "SELECT COALESCE(SUM(reserved), 0) FROM spend").fetchone()[0]
            self._conn.execute("UPDATE spend SET settled = settled + reserved, reserved = 0 "
                               "WHERE reserved > 0")
            self._conn.commit()
        return float(total)

    def record_platform_call(self, tenant_id: str, at: float) -> None:
        """Recorded before any refusal, so a tenant whose calls are all
        refused still counts as free for the turn limit. Never moves
        backwards: calls can land out of order and the turn rule reads "a
        platform call in the past hour"."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO platform_call (tenant_id, at) VALUES (?, ?) "
                "ON CONFLICT(tenant_id) DO UPDATE SET at = max(at, excluded.at)",
                (tenant_id, at))
            self._conn.commit()

    def last_platform_call(self, tenant_id: str) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT at FROM platform_call WHERE tenant_id = ?", (tenant_id,)).fetchone()
        return row[0] if row else None
