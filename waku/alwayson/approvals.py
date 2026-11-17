"""Approvals: a tool call that needs a yes waits for it (spec 023, decision 4).

Each tool has a mode. `allow` runs. `ask` does not run under `waku serve`:

  park     the call is stored in the run's row with a SHA-256 hash of its name
           and arguments, one outbox row asks the owner, and the run ends
           `parked`. The model reads that the call waits.
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
    """Whether an answer from this address names one person. A Telegram chat
    with a positive id is a private chat, and a Slack channel whose id starts
    with D is a direct message. A group chat, a channel and a GitHub issue are
    read by several people, so no yes counts there."""
    channel, target = address
    if channel == "telegram":
        chat_id = target.get("chat_id")
        return isinstance(chat_id, int) and chat_id > 0
    if channel == "slack":
        return str(target.get("channel", "")).startswith("D") and "thread_ts" not in target
    return False


# ---- park ------------------------------------------------------------------

REQUEST = "{what}\n\nApprove? Reply yes or no. [{code}]"
WAITS = ("This call waits for the owner's approval and has not run. The owner was asked "
         "on {where}. Do not make the call again: when the owner answers, this "
         "conversation is told what happened.")
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
    return WAITS.format(where=outbox.format_address(channel, target))
