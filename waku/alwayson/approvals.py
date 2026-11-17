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

from collections.abc import Callable

from waku.alwayson import outbox

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
