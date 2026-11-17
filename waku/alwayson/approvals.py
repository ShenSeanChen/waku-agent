"""Approvals: a tool call that needs a yes waits for it (spec 023, decision 4).

Each tool has a mode. `allow` runs. `ask` does not run under `waku serve`:

  park     the call is stored in the run's row with a SHA-256 hash of its name
           and arguments, one outbox row asks the owner, and the run ends
           `parked`. The model reads that the call waits, and the run sends
           no reply of its own: the request is the one message.
  answer   the owner's answer arrives as an ordinary event. The worker reads
           it before it starts a run: `yes` runs the stored call exactly as
           stored, `no` discards it, anything else is an ordinary message.
  resume   the outcome becomes an event in the conversation that parked, and
           the next run there starts with it.

The model is not asked again after a yes, so it cannot change the arguments.
One call waits per conversation, and it waits 72 hours.

Who answers is one function, `who_may_approve`. This package ships one answer,
the owner in WAKU_OWNER.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from waku.alwayson import outbox, runs

ALLOW, ASK = "allow", "ask"
Address = tuple[str, dict]


def _send_message(args: dict, address: Address | None) -> str:
    """A message to the owner, or back into the conversation the run came
    from, is a reply. A message to anyone else is a write to a third party."""
    to = str(args.get("to", "")).strip()
    if to.lower() in ("owner", "me"):
        return ALLOW
    target = outbox.parse_address(to)
    if target is not None and (target in outbox.owner_addresses() or target == address):
        return ALLOW
    return ASK


# The built-in table. A tool that reads, or that writes only to the person's
# own machine and accounts, is `allow`. A tool that writes to a third party is
# `ask`. A rule that depends on the arguments is a function of them.
MODES: dict[str, str | Callable[[dict, Address | None], str]] = {
    "create_event": ALLOW,
    "list_events": ALLOW,
    "save_note": ALLOW,
    "schedule_task": ALLOW,
    "search_web": ALLOW,
    "manage_memory": ALLOW,
    "update_soul": ALLOW,
    "create_skill": ALLOW,
    "github_read": ALLOW,
    "delegate_task": ALLOW,
    "run_command": ALLOW,      # a skeleton: it runs nothing yet
    "browse_web": ALLOW,       # a skeleton: it opens nothing yet
    "read_apple_calendar": ALLOW,
    "read_apple_mail": ALLOW,
    "create_reminder": ALLOW,
    "create_note": ALLOW,
    "send_message": _send_message,
}


def mode(tool: str, args: dict, address: Address | None = None) -> str:
    """`allow` or `ask` for one call. WAKU_APPROVE_ASK and WAKU_APPROVE_ALLOW
    each name tools that skip the table, and `ask` wins when a tool is in
    both. A tool the table does not know, which is a tool from an MCP server,
    is `allow` until WAKU_APPROVE_ASK names it."""
    from waku.config import approval_overrides

    allow, ask = approval_overrides()
    if tool in ask:
        return ASK
    if tool in allow:
        return ALLOW
    rule = MODES.get(tool, ALLOW)
    return rule(args if isinstance(args, dict) else {}, address) if callable(rule) else rule


def who_may_approve(tool: str, args: dict) -> list[Address]:
    """Where the people who may approve this call are reached. v1 has one
    answer for every call: the owner, at each address WAKU_OWNER names. A team
    layer with several approvers replaces this function; nothing else in this
    package decides who approves."""
    return outbox.owner_addresses()


# ---- what a call says about itself -----------------------------------------

def _describe_send_message(args: dict) -> str:
    to, body = str(args.get("to", "")).strip(), str(args.get("body", "")).strip()
    target = outbox.parse_address(to)
    if target is not None and target[0] == "github":
        return f'Comment on {target[1]["repo"]}#{target[1]["number"]}: "{body}"'
    return f'Send to {to}: "{body}"'


# What a call will do, in the tool's own words. The owner reads this.
DESCRIBE: dict[str, Callable[[dict], str]] = {"send_message": _describe_send_message}
_MAX_DESCRIPTION_CHARS = 1500


def describe(tool: str, args: dict) -> str:
    words = DESCRIBE.get(tool)
    text = words(args) if words else f"Run {tool} with {json.dumps(args, ensure_ascii=False)}"
    return text[:_MAX_DESCRIPTION_CHARS]


def call_hash(tool: str, args: dict) -> str:
    """SHA-256 of the tool name and the arguments. A yes is bound to it."""
    exact = json.dumps({"tool": tool, "args": args}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(exact.encode()).hexdigest()


def can_answer(address: Address) -> bool:
    """Whether a yes from this address names one person and reaches `waku
    serve`. A Telegram chat with a positive id is a private chat. A group
    chat, a Slack channel and a GitHub issue are read by several people, so no
    yes counts there. A Slack direct message has one person, but the Slack
    source hears mentions in channels only, so an answer there never arrives."""
    channel, target = address
    chat_id = target.get("chat_id")
    return channel == "telegram" and isinstance(chat_id, int) and chat_id > 0


def asked_line() -> str:
    """The line a conversation reads when its call waits for an owner who
    answers somewhere else. WAKU_OWNER_NAME is how the owner is called."""
    return ASKED.format(owner=outbox.owner_name() or "the owner")


# ---- park ------------------------------------------------------------------

REQUEST = "{what}\n\nApprove? Reply yes or no. [{code}]"
# What the model reads in place of the tool's result. The request the owner
# got is the one message this run sends (spec 024, decision 1), so the model
# is told to say nothing, and it is given no address to quote.
WAITS = ("This call waits for the owner's approval and has not run. The request is with "
         "the owner, and it is the only message this needs. Do not make the call again, "
         "and do not describe the request: reply with the single word SILENT. When the "
         "owner answers, this conversation is told what happened.")
# What a conversation other than the owner's chat reads when its run parked.
# Code writes this line; the model's own reply is not sent.
ASKED = "I asked {owner} to approve this. I will say here what {owner} decides."
ALREADY_WAITS = ("Not run: another call in this conversation already waits for the owner's "
                 "approval [{code}]. One call waits at a time.")
NOBODY = ("Not run: this call needs the owner's approval, and WAKU_OWNER names no chat "
          "where the owner can answer. Set WAKU_OWNER in .env to telegram:<chat id>.")


class Gate:
    """The check before each tool call of a run under `waku serve`. It has
    the shape of the loop's `before_tool`: (a reason to refuse, or None; "").
    Outside a run it lets every call through, because only the worker can
    bring a parked call back."""

    def __init__(self, home: Path, conn: sqlite3.Connection,
                 now: Callable[[], datetime] = outbox.utc_now) -> None:
        self.home, self.conn, self._now = home, conn, now

    def __call__(self, tool: str, args: dict) -> tuple[str | None, str]:
        context = outbox.current_run(self.home)
        if context is None or not context.run_id.isdigit():
            return None, ""
        args = args if isinstance(args, dict) else {}
        if mode(tool, args, context.address) == ALLOW:
            return None, ""
        return park(self.conn, int(context.run_id), tool, args, self._now()), ""


def park(conn: sqlite3.Connection, run_id: int, tool: str, args: dict, now: datetime) -> str:
    """Store the call in its run's row and ask the owner. Returns what the
    model reads in place of the tool's result."""
    run = runs.get(conn, run_id)
    digest = call_hash(tool, args)
    runs.expire(conn, now)
    waiting = [r.pending_hash for r in runs.parked(conn, run.session_id) if r.id != run.id]
    if run.pending_hash and run.pending_hash != digest:
        waiting.append(run.pending_hash)
    if waiting:
        return ALREADY_WAITS.format(code=waiting[0][:8])
    asked = next((a for a in who_may_approve(tool, args) if can_answer(a)), None)
    if asked is None:
        return NOBODY
    channel, target = asked
    runs.set_pending(conn, run.id, {"tool": tool, "args": args,
                                    "asked": {"channel": channel, "target": target}}, digest)
    # One request per run: a replayed run asks with the same key.
    outbox.insert(conn, run_id=str(run.id), channel=channel, target=target,
                  body=REQUEST.format(what=describe(tool, args), code=digest[:8]),
                  idempotency_key=f"{run.id}:ask", now=now)
    return WAITS


# ---- the answer ------------------------------------------------------------

SOURCE = "approval"
_ANSWER = re.compile(r"^(yes|approve|approved|no|deny|denied)(?:\s+\[?([0-9a-f]{8})\]?)?$")
APPROVED = "Approved and done: {what} The tool answered: {output}"
DECLINED = "Declined and discarded: {what} The owner answered no, so the call did not run."
BROKEN = ("Approved, and not run: {what} The stored call no longer matches its hash, so it "
          "was discarded.")
WHICH = "Several calls wait for your answer. Reply yes or no with the code of one:\n{calls}"


def read_answer(text: str) -> tuple[bool, str] | None:
    """(yes or no, the 8-character code or "") when the whole message is an
    answer, None when it is anything else."""
    found = _ANSWER.match((text or "").strip().strip(".!").strip().lower())
    if found is None:
        return None
    return found[1].startswith(("yes", "approve")), found[2] or ""


def is_approver(event, run: runs.Run) -> bool:
    """Whether this event is an answer to this parked run's request from a
    person who may approve it: it came in at the address the request went to,
    that address is still one `who_may_approve` names, and its sender is the
    one person a chat at that address holds."""
    call = run.pending_call or {}
    asked = call.get("asked") or {}
    address = (asked.get("channel"), asked.get("target"))
    if (event.source, event.payload.get("reply_to")) != address or not can_answer(address):
        return False
    if address not in who_may_approve(call.get("tool", ""), call.get("args") or {}):
        return False
    # In a private Telegram chat the chat's id is the person's id.
    sender = str((event.payload.get("sender") or {}).get("id", ""))
    return sender == str(address[1].get("chat_id"))


def waiting_for(conn: sqlite3.Connection, event, now: datetime) -> list[runs.Run]:
    """The parked runs this event's sender may answer, oldest first."""
    runs.expire(conn, now)
    return [run for run in runs.parked(conn) if is_approver(event, run)]


def intact(run: runs.Run) -> bool:
    """Whether the stored call is the one that was hashed when it parked."""
    call = run.pending_call or {}
    return call_hash(call.get("tool", ""), call.get("args")) == run.pending_hash


def outcome(run: runs.Run, approved: bool, output: str = "") -> str:
    """The first line of the next run in the conversation that parked."""
    call = run.pending_call or {}
    what = describe(call.get("tool", ""), call.get("args") or {})
    if not approved:
        return DECLINED.format(what=what)
    if not intact(run):
        return BROKEN.format(what=what)
    return APPROVED.format(what=what, output=output)


def which(waiting: list[runs.Run]) -> str:
    """What the owner reads when a bare yes or no could mean several calls."""
    calls = "\n".join(
        f"[{run.pending_hash[:8]}] "
        f"{describe(run.pending_call['tool'], run.pending_call['args'])}" for run in waiting)
    return WHICH.format(calls=calls)
