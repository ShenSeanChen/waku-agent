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

Who answers is one function, `who_may_approve`. For every call it names the
owner in WAKU_OWNER. A drafted pull request review is asked first in the
digest channel, where a click counts from a person WAKU_APPROVERS maps to a
GitHub login with write access (spec 035).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from fnmatch import fnmatchcase
from pathlib import Path

from waku.alwayson import outbox, runs

ALLOW, ASK = "allow", "ask"
Address = tuple[str, dict]


def is_private(address: Address) -> bool:
    """Whether only the people a message is meant for read this place: an
    address WAKU_OWNER names, a Telegram chat with one person, whose id is
    positive, or the Slack direct message of a hosted agent's own person. A
    GitHub issue, a Slack channel and a group chat are read by others."""
    channel, target = address
    chat_id = target.get("chat_id")
    return (address in outbox.owner_addresses()
            or (channel == "telegram" and isinstance(chat_id, int) and chat_id > 0)
            or own_slack_chat(address))


def own_slack_chat(address: Address) -> bool:
    """A Slack direct message between a hosted agent and the person it
    belongs to (spec 032, version A).

    A direct-message id starts with `D`, and that alone says nothing: anybody
    in a workspace can open a chat with the bot. What says it is `private` in
    the event's `reply_to`, which the platform's gateway sets after it matched
    the sender to the person who added the app. The mark arrives with an event
    the gateway hands over, and a timer set in that chat carries it on. No
    tool can write it: an address a model names is parsed from text
    (outbox.parse_address), and the parsed form has no such field."""
    channel, target = address
    return (channel == "slack" and target.get("private") is True
            and str(target.get("channel") or "").startswith("D")
            and os.getenv("WAKU_HOSTED", "") == "1")


def reply_is_free(address: Address) -> bool:
    """Whether a run's reply goes to this place without a yes (spec 024,
    decision 5). A reply to a private place does, and so does a Slack reply
    on hosted Waku. Any other reply that other people read is a write, unless
    WAKU_APPROVE_ALLOW names `reply:<channel>`, for example `reply:github`
    for a person's own repositories."""
    from waku.config import approval_overrides

    return (is_private(address) or _hosted_slack(address)
            or f"reply:{address[0]}" in approval_overrides()[0])


def _hosted_slack(address: Address) -> bool:
    """A Slack reply from a hosted container. The person pressed "Add to
    Slack" so that the agent answers where it is mentioned, and a hosted
    container has no chat where an owner could say yes. On 2026-11-18 the
    first mention on hosted Waku was answered and the answer was never sent:
    it waited for an approval nobody could give. A container sends to Slack
    only through the platform, to the one workspace its person connected."""
    return address[0] == "slack" and os.getenv("WAKU_HOSTED", "") == "1"


# ---- the PR desk's channel (spec 035) ----------------------------------------

_CHANNEL_RE = re.compile(r"^[A-Z0-9]+$")


def digest_channel() -> str:
    """WAKU_DIGEST_CHANNEL: the Slack channel id the PR desk posts its morning
    digest to, `#dev` for the maintainers. Empty when it is not set or is not
    a channel id."""
    channel = os.getenv("WAKU_DIGEST_CHANNEL", "").strip()
    return channel if _CHANNEL_RE.match(channel) else ""


def digest_address() -> Address | None:
    """Where the digest goes: the channel itself, not a thread. `digest`
    marks the row, so a drafted review can wait in a thread under it."""
    channel = digest_channel()
    return ("slack", {"channel": channel, "digest": True}) if channel else None


# ---- who approves a drafted review (spec 035, decision 1) -------------------

# The repository roles that may approve. GitHub's `role_name` says maintain
# and triage; its older `permission` field folds them into write and read.
WRITE_ROLES = frozenset({"write", "maintain", "admin"})
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_SLACK_USER_RE = re.compile(r"^[UW][A-Z0-9]+$")
# The calls asked in the digest channel when approvers are mapped. A review
# names its repository, so GitHub can say who may approve it.
SLACK_APPROVED = frozenset({"github_review"})


def approvers(env=None) -> dict[str, str]:
    """WAKU_APPROVERS, `<slack user id>:<github login>,...`, as a mapping. A
    pair of another shape is dropped here, so no value reaches `gh` unchecked."""
    env = os.environ if env is None else env
    found: dict[str, str] = {}
    for pair in env.get("WAKU_APPROVERS", "").split(","):
        slack_id, _, login = (part.strip() for part in pair.partition(":"))
        if _SLACK_USER_RE.match(slack_id) and _LOGIN_RE.match(login):
            found[slack_id] = login
    return found


def permission(repo: str, login: str, run=None) -> str:
    """The login's role on the repository, as GitHub says it now, or "" when
    `gh` could not say. It is asked at every click and never kept, so a person
    removed from the repository loses approval at once."""
    from waku.tools import github

    if not github._REPO_RE.match(repo or "") or not _LOGIN_RE.match(login or ""):
        return ""
    ok, text = (run or github._run)(
        ["gh", "api", f"repos/{repo}/collaborators/{login}/permission"])
    if not ok:
        return ""
    try:
        answer = json.loads(text or "{}")
    except ValueError:
        return ""
    if not isinstance(answer, dict):
        return ""
    return str(answer.get("role_name") or answer.get("permission") or "").lower()


def may_approve(slack_id: str, repo: str, run=None) -> bool:
    """Whether a click from this Slack user counts for a review of this
    repository: WAKU_APPROVERS maps the user to a login, and GitHub says the
    login may write to the repository."""
    login = approvers().get(slack_id)
    return login is not None and permission(repo, login, run) in WRITE_ROLES


def approval_place() -> Address | None:
    """The Slack channel where a drafted review waits for a click: the digest
    channel, once WAKU_APPROVERS maps at least one person."""
    channel = digest_channel()
    return ("slack", {"channel": channel}) if channel and approvers() else None


def _send_message(args: dict, address: Address | None) -> str:
    """A message to the owner is a reply. So is a message back into the
    conversation the run came from, when the run's own reply would go there
    without a yes. A message to anyone else is a write to a third party."""
    to = str(args.get("to", "")).strip()
    if to.lower() in ("owner", "me"):
        return ALLOW
    target = outbox.parse_address(to)
    if target is not None and (target in outbox.owner_addresses()
                               or (target == address and reply_is_free(address))
                               or _same_slack_channel(target, address)):
        return ALLOW
    return ASK


def _same_slack_channel(target: Address, address: Address | None) -> bool:
    """A message to the channel a hosted run's Slack thread is in. The same
    people read the channel and its thread, so it is a reply by another
    route: "remind me in the channel, not in this thread"."""
    return (address is not None and target[0] == "slack" == address[0]
            and _hosted_slack(address)
            and bool(target[1].get("channel"))
            and target[1].get("channel") == address[1].get("channel"))


def _connect_tool(args: dict, address: Address | None) -> str:
    """A call the tool would refuse anyway (a command, not an address) runs,
    so the tool says why and the owner is not asked. Every other call asks,
    and so does every call that changes whether a connector's writes ask."""
    from waku.tools import connectors

    if connectors.hosted():
        # On hosted Waku the tool connects nothing: it answers with the link
        # to the platform's sign-in page, or says what is not built
        # (connectors.hosted_connect). Nobody there could say yes.
        return ALLOW
    if str(args.get("writes") or "").strip():
        return ASK
    try:
        connectors.resolve(str(args.get("name_or_url", "")))
    except connectors.Refused:
        return ALLOW
    return ASK


def _disconnect_tool(args: dict, address: Address | None) -> str:
    """It asks, except on hosted Waku, where the tool removes nothing and
    says where a connector is disconnected."""
    from waku.tools import connectors

    return ALLOW if connectors.hosted() else ASK


# The tools whose mode nothing in the environment changes. Adding or removing
# a server always takes the owner's yes (spec 025, decision 1), and a drafted
# review always waits for a maintainer (spec 035, decision 2).
ALWAYS_ASK = frozenset({"connect_tool", "disconnect_tool", "github_review"})

# The built-in table. A tool that reads, or that writes only to the person's
# own machine and accounts, is `allow`. A tool that writes to a third party is
# `ask`. A rule that depends on the arguments is a function of them.
MODES: dict[str, str | Callable[[dict, Address | None], str]] = {
    "create_event": ALLOW,
    "list_events": ALLOW,
    "save_note": ALLOW,
    "schedule_task": ALLOW,
    "list_waiting": ALLOW,
    "cancel_waiting": ALLOW,   # it cancels the person's own timers and requests
    "search_web": ALLOW,
    "manage_memory": ALLOW,
    "update_soul": ALLOW,
    "create_skill": ALLOW,
    "github_read": ALLOW,
    # A review posted on a pull request, as a comment (spec 035). In
    # ALWAYS_ASK too, so WAKU_APPROVE_ALLOW cannot turn it into a write that
    # runs unasked.
    "github_review": ASK,
    "delegate_task": ALLOW,
    "run_command": ALLOW,      # a skeleton: it runs nothing yet
    "browse_web": ALLOW,       # a skeleton: it opens nothing yet
    "read_apple_calendar": ALLOW,
    "read_apple_mail": ALLOW,
    "create_reminder": ALLOW,
    "create_note": ALLOW,
    "send_message": _send_message,
    "connect_tool": _connect_tool,
    "disconnect_tool": _disconnect_tool,
    "connector_status": ALLOW,   # it reads this machine's own files
    # It reads the channel the run is in, which the people there already read
    # (spec 029, decision 5). Hosted Waku only.
    "slack_history": ALLOW,
    # The person's own mailbox on hosted Waku (spec 031 A). Two read it. The
    # third writes a draft there, which reaches nobody until the person
    # presses Send in Gmail themselves. No tool sends.
    "mail_search": ALLOW,
    "mail_read": ALLOW,
    "mail_draft": ALLOW,
}


def rule_for(server: str) -> str:
    """The stored rule that covers every tool of one connector."""
    return f"{server}_*"


def stored(conn: sqlite3.Connection) -> tuple[str, ...]:
    """The rules the owner stored: patterns of tools that run without a
    question (spec 025, decision 4)."""
    return tuple(row[0] for row in conn.execute(
        "SELECT pattern FROM approvals WHERE mode = ? ORDER BY pattern", (ALLOW,)))


def store(conn: sqlite3.Connection, server: str, allow: bool,
          now: datetime | None = None) -> None:
    """Let every tool of one connector run without a question, or take that
    back."""
    if allow:
        conn.execute("INSERT OR REPLACE INTO approvals (pattern, mode, created_at) "
                     "VALUES (?, ?, ?)", (rule_for(server), ALLOW,
                                          outbox._stamp(now or outbox.utc_now())))
    else:
        conn.execute("DELETE FROM approvals WHERE pattern = ?", (rule_for(server),))
    conn.commit()


def mode(tool: str, args: dict, address: Address | None = None,
         read_only: bool = False, connector=None, own_name: str = "",
         allowed: tuple[str, ...] = ()) -> str:
    """`allow` or `ask` for one call, decided in this order:

      1. connect_tool, disconnect_tool and github_review ask, whatever comes next.
      2. WAKU_APPROVE_ASK or WAKU_APPROVE_ALLOW names the tool: that wins, and
         `ask` wins when a tool is in both.
      3. The built-in table knows the tool.

    Every other tool comes from an MCP server (spec 024, decision 6, and spec
    025, decision 4). `connector` says which kind of server, `own_name` is
    the tool's name there, and `allowed` holds the rules the owner stored:

      4. The owner let this connector write without asking: the tool runs.
      5. Waku Memory: the tool runs. Saving a memory is the agent's own job.
         A connector the hosted platform calls for this container (spec 029):
         the tool runs here, and the platform refuses the ones that write,
         because nobody on hosted Waku can be asked yet.
      6. A server on the starter list: a tool matching the entry's `writes`
         asks, and every other tool runs. The list decides, not the server.
      7. Any other server: a tool the server marks read-only (`readOnlyHint`)
         runs. The mark is the server's own claim, so it counts only for a
         server the owner added themselves.
      8. Otherwise it asks.
    """
    from waku.config import approval_overrides

    args = args if isinstance(args, dict) else {}
    if tool in ALWAYS_ASK:
        rule = MODES[tool]
        return rule(args, address) if callable(rule) else rule
    allow, ask = approval_overrides()
    if tool in ask:
        return ASK
    if tool in allow:
        return ALLOW
    if tool in MODES:
        rule = MODES[tool]
        return rule(args, address) if callable(rule) else rule
    kind = getattr(connector, "kind", "")
    if connector is not None and rule_for(connector.name) in allowed:
        return ALLOW
    if kind == "memory":
        return ALLOW
    if kind == "platform":
        return ALLOW
    if kind == "starter":
        writes = any(fnmatchcase(own_name or tool, pattern) for pattern in connector.writes)
        return ASK if writes else ALLOW
    return ALLOW if read_only else ASK


def who_may_approve(tool: str, args: dict) -> list[Address]:
    """Where the people who may approve this call are reached. A drafted
    review (spec 035) is asked first in the digest channel, where a click
    counts from a person WAKU_APPROVERS maps to a GitHub login with write
    access (`may_approve`). Every call also names the owner, at each address
    WAKU_OWNER names. A team layer replaces this function; nothing else in
    this package decides who approves."""
    place = approval_place() if tool in SLACK_APPROVED else None
    return ([place] if place else []) + outbox.owner_addresses()


# ---- what a call says about itself -----------------------------------------

def _describe_send_message(args: dict) -> str:
    to, body = str(args.get("to", "")).strip(), str(args.get("body", "")).strip()
    target = outbox.parse_address(to)
    if target is not None and target[0] == "github":
        return f'Comment on {target[1]["repo"]}#{target[1]["number"]}: "{body}"'
    return f'Send to {to}: "{body}"'


def _describe_connect_tool(args: dict) -> str:
    from waku.tools import connectors

    target = str(args.get("name_or_url", "")).strip()
    writes = str(args.get("writes") or "").strip().lower()
    if writes == "allow":
        return f"Let {target}'s tools write without asking first."
    if writes:
        return f"Make {target}'s tools that write ask first again."
    listed = connectors.starter().get(target.lower())
    if listed is not None:
        return (f"Connect {listed['title']} ({listed['url']}). Waku will be able to "
                "call its tools.")
    return f"Connect the MCP server at {target}. Waku will be able to call its tools."


def _describe_disconnect_tool(args: dict) -> str:
    return f"Disconnect '{str(args.get('name', '')).strip()}' and forget its sign-in."


_MAX_VALUE_CHARS = 300


def _describe_any(tool: str, args: dict) -> str:
    """A tool with no words of its own: its name, then one line per
    argument. The owner reads this on a phone, so it is not one line of JSON."""
    if not args:
        return f"Run {tool}."
    lines = [f"Run {tool} with:"]
    for key, value in args.items():
        said = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if len(said) > _MAX_VALUE_CHARS:
            said = said[:_MAX_VALUE_CHARS] + "…"
        lines.append(f"• {key}: {said}")
    return "\n".join(lines)


def _describe_github_review(args: dict) -> str:
    body = str(args.get("body", "")).strip()
    short = body if len(body) <= 200 else body[:200] + "…"
    return f'Review on {args.get("repo")}#{args.get("number")}, as a comment: "{short}"'


# What a call will do, in the tool's own words. The owner reads this.
DESCRIBE: dict[str, Callable[[dict], str]] = {
    "send_message": _describe_send_message,
    "connect_tool": _describe_connect_tool,
    "disconnect_tool": _describe_disconnect_tool,
    "github_review": _describe_github_review,
}
_MAX_DESCRIPTION_CHARS = 1500


def describe(tool: str, args: dict) -> str:
    words = DESCRIBE.get(tool)
    text = words(args) if words else _describe_any(tool, args)
    return text[:_MAX_DESCRIPTION_CHARS]


def call_hash(tool: str, args: dict) -> str:
    """SHA-256 of the tool name and the arguments. A yes is bound to it."""
    exact = json.dumps({"tool": tool, "args": args}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(exact.encode()).hexdigest()


def can_answer(address: Address) -> bool:
    """Whether an answer from this address names one person and reaches
    `waku serve`. A Telegram chat with a positive id is a private chat. The
    digest channel counts once approvers are mapped, because there a button
    names the person who pressed it (spec 035); a typed yes there still does
    not count. A group chat, any other Slack channel and a GitHub issue are
    read by several people, so no yes counts there. A Slack direct message has
    one person, but the Slack source hears mentions in channels only, so an
    answer there never arrives."""
    channel, target = address
    if channel == "slack":
        place = approval_place()
        return place is not None and target.get("channel") == place[1]["channel"]
    chat_id = target.get("chat_id")
    return channel == "telegram" and isinstance(chat_id, int) and chat_id > 0


def is_owner_chat(address: Address | None) -> bool:
    """Whether the one person who reads this place is the owner: an address
    WAKU_OWNER names, where a yes counts."""
    return address is not None and address in outbox.owner_addresses() and can_answer(address)


def asked_line() -> str:
    """The line a conversation reads when its call waits for an owner who
    answers somewhere else. WAKU_OWNER_NAME is how the owner is called."""
    return ASKED.format(owner=outbox.owner_name() or "the owner")


# ---- park ------------------------------------------------------------------

REQUEST = "{what}\n\n{before}Approve? Reply yes or no. [{code}]"
# The same request where it is answered with buttons (spec 035).
REQUEST_BUTTONS = "{what}\n\n{before}Approve or Reject with the buttons below. [{code}]"


@dataclass(frozen=True)
class Request:
    """What a person is asked to approve (spec 035, decision 5). The five
    fields are the same for every channel: the Slack sender draws them as
    blocks with two buttons, Telegram reads them as text, and a later sender
    for the Waku board can draw a `decide` message from the same fields."""
    title: str
    why: str
    link: str
    body: str
    code: str
    before: str = ""
    choices: tuple[str, str] = ("Approve", "Reject")

    def what(self) -> str:
        head = "\n".join(part for part in (self.title, self.why, self.link) if part)
        return f"{head}\n\n{self.body}" if self.body else head

    def text(self, channel: str = "telegram") -> str:
        words = REQUEST_BUTTONS if channel == "slack" else REQUEST
        return words.format(what=self.what(), before=self.before, code=self.code)

    def fields(self) -> dict:
        return {"title": self.title, "why": self.why, "link": self.link, "body": self.body,
                "choices": list(self.choices), "code": self.code}


def _request_github_review(args: dict) -> tuple[str, str, str, str]:
    repo, number = str(args.get("repo", "")), args.get("number")
    return (f"Review PR #{number}",
            (f"A drafted review of {repo}#{number}. Approve posts it on the pull request "
             "as a comment, and Reject discards it."),
            f"https://github.com/{repo}/pull/{number}", str(args.get("body", "")))


# Tools whose request has a title, a reason, a link and a body of its own.
REQUESTS: dict[str, Callable[[dict], tuple[str, str, str, str]]] = {
    "github_review": _request_github_review,
}


def request(tool: str, args: dict, digest: str, before: str = "") -> Request:
    """The request for one call. A tool without words of its own is asked
    with its description as the title, as before spec 035."""
    words = REQUESTS.get(tool)
    title, why, link, body = words(args) if words else (describe(tool, args), "", "", "")
    return Request(title, why, link, body, digest[:8], before)
# One more line in a request for a call the owner approved in the last 24
# hours (spec 024, decision 4). The owner still decides: the same request can
# be meant twice, and it can be a mistake.
RAN_BEFORE = "The same call ran at {time} {day}.\n\n"
RAN_BEFORE_HOURS = 24
# What the model reads in place of the tool's result. The request the owner
# got is the one message this run sends (spec 024, decision 1), so the model
# is told to say nothing, and it is given no address to quote.
WAITS = ("This call waits for the owner's approval and has not run. The request is with "
         "the owner, and it is the only message this needs. Do not make the call again, "
         "and do not describe the request: reply with the single word SILENT. When the "
         "owner answers, this conversation is told what happened.")
# The same, when the run is in the owner's own chat: the person the model
# talks to is the one who was asked, and the model must not call them "the
# owner" as if they were someone else (the live run of 2026-11-17).
WAITS_YOU = ("This call waits for approval and has not run. The person you are talking to "
             "has been asked; they answer yes or no in this chat. The request is the only "
             "message this needs. Do not make the call again, and do not describe the "
             "request: reply with the single word SILENT. When they answer, this "
             "conversation is told what happened.")
# The same, in the PR desk's morning run (spec 035). Its reply is the digest
# and is still posted, and the review waits in a thread under it.
WAITS_DESK = ("This review waits for a maintainer's approval and has not been posted. It is "
              "shown in a thread under your digest, with Approve and Reject. Do not draft it "
              "again. Finish the digest: your reply is still posted.")
# What a conversation other than the owner's chat reads when its run parked.
# Code writes this line; the model's own reply is not sent.
ASKED = "I asked {owner} to approve this. I will say here what {owner} decides."
ALREADY_WAITS = ("Not run: another call in this conversation already waits for approval "
                 "[{code}]. One call waits at a time.")
NOBODY = ("Not run: this call needs the owner's approval, and WAKU_OWNER names no chat "
          "where the owner can answer. Set WAKU_OWNER in .env to telegram:<chat id>.")
# What the model reads when the PR desk's run calls any tool other than
# worker.DESK_TOOLS. A pull request's text reaches that run, and anyone can
# open one (spec 035, decision 2). Like every text here, it names no tool.
OUTSIDE_DESK = ("Not run: this run reads GitHub and drafts one review, and it can do "
                "nothing else.")


class Gate:
    """The check before each tool call of a run under `waku serve`. It has
    the shape of the loop's `before_tool`: (a reason to refuse, or None; "").
    Outside a run it lets every call through, because only the worker can
    bring a parked call back."""

    def __init__(self, home: Path, conn: sqlite3.Connection,
                 now: Callable[[], datetime] = outbox.utc_now, tz: tzinfo | None = None,
                 tools=None) -> None:
        # `tz` is for the evals: the zone a time in a request is said in.
        # None is the zone of this machine, which is the owner's.
        self.home, self.conn, self._now, self._tz = home, conn, now, tz
        # The agent's tool registry, which knows what each tool declared.
        self._tools = tools

    def __call__(self, tool: str, args: dict) -> tuple[str | None, str]:
        context = outbox.current_run(self.home)
        if context is None or not context.run_id.isdigit():
            return None, ""
        args = args if isinstance(args, dict) else {}
        if context.only is not None and tool not in context.only:
            return OUTSIDE_DESK, ""
        known = self._tools.get(tool) if self._tools is not None else None
        if self._tools is not None and known is None and tool not in MODES:
            # No such tool, so there is nothing to approve. The registry tells
            # the model so. On 2026-11-18 the model called a Notion tool under
            # a name it made up, and the owner was asked to approve a search.
            return None, ""
        if self.mode(tool, args, context.address, known) == ALLOW:
            return None, ""
        return park(self.conn, int(context.run_id), tool, args, self._now(), self._tz,
                    address=context.address), ""


    def mode(self, tool: str, args: dict, address: Address | None, known) -> str:
        """`mode` for a tool of this agent: the registry says which MCP
        server it comes from, and mcp.json says which kind of server that is."""
        server, own_name = getattr(known, "mcp", None) or ("", "")
        connector = None
        if server:
            from waku.tools import connectors

            connector = connectors.connector_of(self.home, server)
        return mode(tool, args, address, getattr(known, "read_only", False),
                    connector=connector, own_name=own_name,
                    allowed=stored(self.conn) if connector is not None else ())


def ran_before(conn: sqlite3.Connection, digest: str, now: datetime,
               tz: tzinfo | None = None) -> str:
    """The line that says when the same call last ran, or "" when it did not
    run in the last 24 hours."""
    at = runs.ran_at(conn, digest, now - timedelta(hours=RAN_BEFORE_HOURS))
    if at is None:
        return ""
    at, today = at.astimezone(tz), now.astimezone(tz).date()
    return RAN_BEFORE.format(time=f"{at:%H:%M}",
                             day="today" if at.date() == today else "yesterday")


def park(conn: sqlite3.Connection, run_id: int, tool: str, args: dict, now: datetime,
         tz: tzinfo | None = None, reply: bool = False,
         address: Address | None = None) -> str:
    """Store the call in its run's row and ask the owner. Returns what the
    model reads in place of the tool's result. `reply` marks the call as the
    run's own reply, which the worker parks when other people would read it.
    `address` is the conversation the run came from."""
    run = runs.get(conn, run_id)
    digest = call_hash(tool, args)
    # A call that has waited 72 hours no longer counts. The worker marks it
    # expired at its next step and says so.
    waiting = [r.pending_hash for r in runs.parked(conn, run.session_id)
               if r.id != run.id and not runs.overdue(r, now)]
    if run.pending_hash and run.pending_hash != digest:
        waiting.append(run.pending_hash)
    if waiting:
        return ALREADY_WAITS.format(code=waiting[0][:8])
    asked = next((a for a in who_may_approve(tool, args) if can_answer(a)), None)
    if asked is None:
        return NOBODY
    channel, target = asked
    call = {"tool": tool, "args": args, "asked": {"channel": channel, "target": target}}
    if reply:
        call["reply"] = True
    runs.set_pending(conn, run.id, call, digest)
    asked_with = request(tool, args, digest, ran_before(conn, digest, now, tz))
    row_target = target
    if channel == "slack":
        # Drawn with buttons, in a thread under the digest (outbox.resolve_target).
        row_target = {**target, "under_digest": True, "request": asked_with.fields()}
    # One request per run: a replayed run asks with the same key.
    outbox.insert(conn, run_id=str(run.id), channel=channel, target=row_target,
                  body=asked_with.text(channel), idempotency_key=f"{run.id}:ask", now=now)
    if address is not None and address == digest_address():
        return WAITS_DESK
    return WAITS_YOU if asked == address else WAITS


# ---- the answer ------------------------------------------------------------

SOURCE = "approval"
_ANSWER = re.compile(r"^(yes|approve|approved|no|deny|denied)(?:\s+\[?([0-9a-f]{8})\]?)?$")
APPROVED = "Approved and done: {what} The tool answered: {output}"
# A message is the one call whose result a person can be told in two words.
POSTED = "Approved and posted: {what}"
NOT_POSTED = "Approved, and not posted: {what} The reason: {output}"
# The outcomes name nobody: the run that reads one may be talking to the owner.
DECLINED = "Declined and discarded: {what} The answer was no, so the call did not run."
BROKEN = ("Approved, and not run: {what} The stored call no longer matches its hash, so it "
          "was discarded.")
# What the owner reads 24 hours before a waiting call expires, and when it
# does (spec 024, decision 8). Each is sent once.
REMINDER = ("Still waiting for your answer on: {what}\n\nIt expires in 24 hours. "
            "Approve? Reply yes or no. [{code}]")
STOPPED = "I stopped waiting for your answer on: {what} Nothing was posted."
# The reminder where the request has buttons (spec 035).
REMINDER_BUTTONS = ("Still waiting for an answer on: {what}\n\nIt expires in 24 hours. "
                    "Approve or Reject with the buttons above. [{code}]")
# What the thread reads after a click (spec 035). The outcome names nobody, so
# the person who clicked is named here.
ANSWERED = "{who} chose {choice}. {outcome}"
# What the request itself says once it is answered, in place of its buttons.
DECIDED = "{choice} by {who}"
EXPIRED = ("Expired and discarded: {what} Nobody answered in 72 hours, so the call did "
           "not run.")
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
    person who may approve it. Asked in Telegram: it came in at the address
    the request went to, that address is still one `who_may_approve` names,
    and its sender is the one person a chat at that address holds. Asked in
    Slack: `_clicked_by_approver`."""
    call = run.pending_call or {}
    asked = call.get("asked") or {}
    address = (asked.get("channel"), asked.get("target"))
    if address[0] == "slack":
        return _clicked_by_approver(event, address, call)
    if (event.source, event.payload.get("reply_to")) != address or not can_answer(address):
        return False
    if address not in who_may_approve(call.get("tool", ""), call.get("args") or {}):
        return False
    # In a private Telegram chat the chat's id is the person's id.
    sender = str((event.payload.get("sender") or {}).get("id", ""))
    return sender == str(address[1].get("chat_id"))


def _clicked_by_approver(event, address: Address, call: dict) -> bool:
    """A click on Approve or Reject in the channel the request went to, by a
    person WAKU_APPROVERS maps to a GitHub login that may write to the
    review's repository right now (spec 035). A typed yes in Slack never
    counts: several people read a channel, and only a button names the person
    who pressed it. The GitHub check runs last, so a typed answer or another
    channel never reaches `gh`."""
    target = address[1] if isinstance(address[1], dict) else {}
    reply_to = event.payload.get("reply_to") or {}
    sender = str((event.payload.get("sender") or {}).get("id", ""))
    return (event.source == "slack" and event.payload.get("clicked") in ("yes", "no")
            and can_answer(address) and reply_to.get("channel") == target.get("channel")
            and may_approve(sender, str((call.get("args") or {}).get("repo", ""))))


def waiting_for(conn: sqlite3.Connection, event, now: datetime) -> list[runs.Run]:
    """The parked runs this event's sender may answer, oldest first."""
    return [run for run in runs.parked(conn)
            if not runs.overdue(run, now) and is_approver(event, run)]


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
    if call.get("tool") == "send_message":
        refused = output.startswith(("Not sent", "Error"))
        return (NOT_POSTED.format(what=what, output=output) if refused
                else POSTED.format(what=what))
    return APPROVED.format(what=what, output=output)


def answered(run: runs.Run, approved: bool, output: str, who: str) -> str:
    """The line in the thread after a click on a request asked in Slack."""
    return ANSWERED.format(who=who or "An approver", choice="Approve" if approved else "Reject",
                           outcome=outcome(run, approved, output))


def decided(approved: bool, who: str) -> str:
    """The line that replaces the buttons on a request answered in Slack."""
    return DECIDED.format(choice="Approved" if approved else "Rejected",
                          who=who or "an approver")


def asked_address(run: runs.Run) -> Address | None:
    """Where the request for this run's call went."""
    asked = (run.pending_call or {}).get("asked") or {}
    channel, target = asked.get("channel"), asked.get("target")
    return (channel, target) if channel in outbox.CHANNELS and isinstance(target, dict) else None


def said(run: runs.Run, words: str) -> str:
    """REMINDER, STOPPED or EXPIRED, filled in for one waiting call."""
    call = run.pending_call or {}
    return words.format(what=describe(call.get("tool", ""), call.get("args") or {}),
                        code=run.pending_hash[:8])


def which(waiting: list[runs.Run]) -> str:
    """What the owner reads when a bare yes or no could mean several calls."""
    calls = "\n".join(
        f"[{run.pending_hash[:8]}] "
        f"{describe(run.pending_call['tool'], run.pending_call['args'])}" for run in waiting)
    return WHICH.format(calls=calls)
