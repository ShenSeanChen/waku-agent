"""A tenant's mailbox, read and drafted in by the gateway (spec 031, part A).

A person signs in to Google from /setup (connect.py, the connector `gmail`).
Their agent then has three tools, mail_search, mail_read and mail_draft. The
container holds no Google token: each tool asks the metering proxy, the proxy
finds the tenant from the container's platform token and asks the gateway
over gateway.sock, and the gateway calls Gmail here with that tenant's token:

  container --POST /mail/read--> proxy --gateway.sock--> gateway --> Gmail

THERE IS NO WAY TO SEND (decision 1, and Sean on 2026-11-19: drafts only, he
presses send). The actions are a closed set of three. GmailApi has one
request that writes, and it creates a draft. No address in this file sends,
deletes, labels or changes a mail, and evals/deterministic/hosted/
test_mail_gmail.py reads this file to keep it so. Google's own scope for
drafts would let a client send one: what stops it is that no code here asks.

A DRAFT IS A REPLY, AND THE GATEWAY ADDRESSES IT. The agent gives a thread
and the text. Who the reply goes to is read from the thread: the last mail in
it that the person did not write, at its Reply-To or its From. The agent
cannot name a recipient, so a mail that says "send this to eve@evil" cannot
put that address on a draft. The draft is made in the mailbox the thread is
in, because there is one sign-in and it is that mailbox's.

ONE MAILBOX PER PERSON, FOR NOW. The spec wants several. control.db keeps
one sign-in per tenant and connector, so a second mailbox needs a change
there, and it is left for the next piece.

A MAIL IS OTHER PEOPLE'S TEXT (decision 4). Everything read from Gmail is
bounded here: a thread's last MAX_THREAD_MESSAGES mails, MAX_BODY_CHARS of
each, one line per header, no control characters. The container's tool puts
it inside a frame that says it is not an instruction (waku/tools/mail.py).

Nothing here logs a token, a query, an address or a mail's text. A log line
carries a tenant id, an action and a status.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import zlib
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formataddr, getaddresses
from html.parser import HTMLParser

import aiohttp

from hosted import log
from hosted.core.connectors import VENDORS
from hosted.core.tenant import is_tenant_id
from hosted.gateway.connect import (
    Connect,
    NotConnected,
    ReconnectNeeded,
    VendorUnreachable,
)

_LOG = log.get(__name__)

SERVER = "gmail"
ACTIONS = frozenset({"search", "read", "draft"})
# One request to Gmail. The container's tool waits 15 seconds for the whole
# answer, and a search is one request and then one per message, side by side.
TIMEOUT = aiohttp.ClientTimeout(total=8, sock_connect=4)
# The most of one answer of Gmail's that is read. A thread with long mails is
# a few hundred kilobytes; an attachment is not in it.
MAX_ANSWER_BYTES = 4 * 1024 * 1024
SEARCH_DEFAULT, SEARCH_MAX = 10, 20
MAX_QUERY_CHARS = 300
# How many of a search's messages are asked for at once.
SIDE_BY_SIDE = 5
# The last mails of a thread that are returned, and the most of each one's
# text. Ten mails of 4,000 characters are about 10,000 tokens for the model.
MAX_THREAD_MESSAGES = 10
MAX_BODY_CHARS = 4000
MAX_HEADER_CHARS = 200
# The longest text of a draft. With JSON's escapes it stays under the 64 KiB
# one request on gateway.sock may be (jsonsock.MAX_LINE).
MAX_DRAFT_CHARS = 8000

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f  ]")
_BLANK_LINES = re.compile(r"\n{3,}")
_GZIP_MAGIC = b"\x1f\x8b"
_API = "/gmail/v1/users/me"
_HEADERS = ("From", "To", "Cc", "Reply-To", "Subject", "Message-ID", "References")


class _Refused(RuntimeError):
    """Gmail answered with a status that is not an answer."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


def one_line(value: object, limit: int = MAX_HEADER_CHARS) -> str:
    """A header's value as one bounded line: a From or a Subject is the
    sender's text, and it must not start a line of its own."""
    text = _CONTROL.sub(" ", str(value or "").replace("\n", " ").replace("\t", " "))
    return " ".join(text.split())[:limit]


class _Text(HTMLParser):
    """The words of an HTML mail, without its tags, styles and scripts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head", "title"):
            self._skip += 1
        elif tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "title") and self._skip:
            self._skip -= 1
        elif tag in ("p", "div", "tr", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _decoded(part: dict) -> str:
    data = (part.get("body") or {}).get("data")
    if not isinstance(data, str) or not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
            "utf-8", "replace")
    except ValueError:
        return ""


def _parts(payload: object, kind: str) -> list[str]:
    """The text of every part of one kind, in the mail's own order. A part
    that is an attachment has a file name and is left out."""
    if not isinstance(payload, dict):
        return []
    found = []
    if payload.get("mimeType") == kind and not payload.get("filename"):
        text = _decoded(payload)
        if text:
            found.append(text)
    for part in payload.get("parts") or []:
        found.extend(_parts(part, kind))
    return found


def body_of(payload: object) -> tuple[str, bool]:
    """(a mail's text, whether it was cut). The plain part when there is
    one, or the words of the HTML part. Lines a reply quotes ("> ...") are
    left out: the mail they quote is in the same thread."""
    plain = _parts(payload, "text/plain")
    if plain:
        text = "\n".join(plain)
    else:
        reader = _Text()
        try:
            reader.feed("\n".join(_parts(payload, "text/html")))
            reader.close()
        except (ValueError, AssertionError):
            pass
        text = "".join(reader.parts)
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
             if not line.lstrip().startswith(">")]
    text = _BLANK_LINES.sub("\n\n", _CONTROL.sub(" ", "\n".join(lines))).strip()
    return text[:MAX_BODY_CHARS], len(text) > MAX_BODY_CHARS


def _header(message: dict, name: str) -> str:
    """One header's raw value, or ""."""
    headers = (message.get("payload") or {}).get("headers")
    for header in headers if isinstance(headers, list) else []:
        if isinstance(header, dict) and str(header.get("name", "")).lower() == name.lower():
            return str(header.get("value") or "")
    return ""


def _when(message: dict) -> str:
    """When Gmail received the mail, in UTC. Gmail's own clock, not the
    Date header, which is the sender's to write."""
    try:
        moment = datetime.fromtimestamp(int(message.get("internalDate")) / 1000, UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def _addresses(value: str) -> list[tuple[str, str]]:
    """(name, address) for each address in a header, with nothing that is
    not an address."""
    return [(one_line(name, 80), address.strip()) for name, address
            in getaddresses([one_line(value, 2000)])
            if re.fullmatch(r"[^@\s<>,;]{1,64}@[^@\s<>,;]{1,255}", address.strip())]


def reply_to(messages: list[dict], mailbox: str) -> list[tuple[str, str]]:
    """Who a reply in this thread goes to: the last mail the person did not
    write, at its Reply-To or else its From. When they wrote every mail, the
    people the last one went to. Never the person's own address."""
    own = mailbox.lower()

    def others(value: str) -> list[tuple[str, str]]:
        return [(name, address) for name, address in _addresses(value)
                if address.lower() != own]

    for message in reversed(messages):
        sender = _addresses(_header(message, "From"))
        if sender and sender[0][1].lower() != own:
            return others(_header(message, "Reply-To")) or others(_header(message, "From"))
    return others(_header(messages[-1], "To")) if messages else []


def reply_message(messages: list[dict], mailbox: str, body: str) -> EmailMessage | None:
    """The reply as a mail, or None when the thread names nobody to write
    to. Every header is built from parsed addresses and one-line values."""
    to = reply_to(messages, mailbox)
    if not to:
        return None
    last = messages[-1]
    subject = one_line(_header(messages[0], "Subject")) or "(no subject)"
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    reply = EmailMessage()
    reply["From"] = mailbox
    reply["To"] = ", ".join(formataddr(pair) for pair in to)
    reply["Subject"] = subject
    message_id = one_line(_header(last, "Message-ID"), 500)
    if message_id:
        references = one_line(_header(last, "References"), 4000).split()
        reply["In-Reply-To"] = message_id
        reply["References"] = " ".join([*[r for r in references if r != message_id],
                                        message_id])
    reply.set_content(body)
    return reply


class GmailApi:
    """The five requests the gateway makes to Gmail. Four read. One writes,
    and what it writes is a draft."""

    def __init__(self, session: aiohttp.ClientSession,
                 base_url: str = VENDORS[SERVER].url) -> None:
        self._session = session
        self._base = base_url.rstrip("/")

    async def _ask(self, method: str, path: str, token: str, **kwargs) -> tuple[int, dict]:
        """(status, the answer as an object). The gateway's one session does
        not decompress (__main__.py), so gzip is asked for and opened here."""
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
                   "Accept-Encoding": "gzip, identity"}
        async with self._session.request(
                method, self._base + path, headers=headers, timeout=TIMEOUT,
                allow_redirects=False, **kwargs) as reply:
            raw = bytearray()
            async for chunk in reply.content.iter_any():
                raw.extend(chunk)
                if len(raw) > MAX_ANSWER_BYTES:
                    raise ValueError("the answer is too long")
            status = reply.status
        if raw[:2] == _GZIP_MAGIC:
            inflate = zlib.decompressobj(wbits=31)
            raw = inflate.decompress(bytes(raw), MAX_ANSWER_BYTES + 1)
            if len(raw) > MAX_ANSWER_BYTES or inflate.unconsumed_tail:
                raise ValueError("the answer is too long")
        try:
            answer = json.loads(bytes(raw).decode("utf-8")) if raw else {}
        except ValueError:
            answer = {}
        return status, answer if isinstance(answer, dict) else {}

    async def profile(self, token: str) -> tuple[int, dict]:
        return await self._ask("GET", f"{_API}/profile", token)

    async def find(self, token: str, query: str, limit: int) -> tuple[int, dict]:
        return await self._ask("GET", f"{_API}/messages", token,
                               params={"q": query, "maxResults": str(limit)})

    async def message(self, token: str, message_id: str) -> tuple[int, dict]:
        params = [("format", "metadata"), *[("metadataHeaders", h) for h in _HEADERS]]
        return await self._ask("GET", f"{_API}/messages/{message_id}", token, params=params)

    async def thread(self, token: str, thread_id: str) -> tuple[int, dict]:
        return await self._ask("GET", f"{_API}/threads/{thread_id}", token,
                               params={"format": "full"})

    async def create_draft(self, token: str, thread_id: str,
                           message: EmailMessage) -> tuple[int, dict]:
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        return await self._ask("POST", f"{_API}/drafts", token,
                               json={"message": {"raw": raw, "threadId": thread_id}})


class Mail:
    """Search, read and draft in one tenant's mailbox.

    WHICH TENANT IS NOT THE CALLER'S CHOICE. The proxy gives the tenant id it
    resolved from the container's own platform token, and the one sign-in
    stored for that tenant is used. No argument names a mailbox.
    """

    def __init__(self, *, connect: Connect, api: GmailApi, apex_host: str) -> None:
        self._connect = connect
        self._api = api
        self._link = f"https://{apex_host}/connect/start?server={SERVER}"
        # tenant id -> the address of the mailbox they signed in to.
        self._mailbox: dict[str, str] = {}

    def forget(self, tenant_id: str) -> None:
        """This person signed in again, maybe to another mailbox, or
        disconnected."""
        self._mailbox.pop(tenant_id, None)

    async def handle(self, tenant_id: object, action: object, args: object) -> dict:
        """One call from a container. {"ok": True, "mailbox": <address>, ...}
        or {"ok": False, "error": <code>}, with `url` when signing in is
        what helps."""
        if (not isinstance(tenant_id, str) or not is_tenant_id(tenant_id)
                or action not in ACTIONS or not isinstance(args, dict)):
            return {"ok": False, "error": "bad_request"}
        if self._connect.vendor(SERVER) is None:
            return {"ok": False, "error": "not_offered"}
        try:
            if action == "search":
                return await self._search(tenant_id, args)
            if action == "read":
                return await self._read(tenant_id, args)
            return await self._draft(tenant_id, args)
        except NotConnected:
            return {"ok": False, "error": "not_connected", "url": self._link}
        except ReconnectNeeded:
            self.forget(tenant_id)
            return {"ok": False, "error": "reconnect", "url": self._link}
        except _Refused as refused:
            _LOG.info("tenant=%s: Gmail answered %s with HTTP %s", tenant_id, action,
                      refused.status)
            if refused.status == 403:
                # The person left a box unticked on Google's page, or the
                # Gmail API is off in the project.
                return {"ok": False, "error": "forbidden", "url": self._link}
            code = {404: "not_found", 429: "rate_limited"}.get(refused.status)
            return {"ok": False, "error": code or "google_unreachable"}
        except (VendorUnreachable, aiohttp.ClientError, TimeoutError, OSError, ValueError,
                EOFError, zlib.error) as exc:
            # The exception's own text is left out: aiohttp can quote a
            # request's headers, and one of them is a token.
            _LOG.warning("tenant=%s: mail %s failed (%s)", tenant_id, action,
                         type(exc).__name__)
            return {"ok": False, "error": "google_unreachable"}

    # --- Gmail, with this person's token ------------------------------------

    async def _call(self, tenant_id: str, request, *args) -> dict:
        """One request with this person's token, renewed once when Gmail
        answers 401 for it. Raises _Refused for any other refusal."""
        token = await self._connect.access_token(tenant_id, SERVER)
        status, answer = await request(token, *args)
        if status == 401:
            token = await self._connect.access_token(tenant_id, SERVER, refused=token)
            status, answer = await request(token, *args)
        if status >= 300:
            raise _Refused(status)
        return answer

    async def _address(self, tenant_id: str) -> str:
        known = self._mailbox.get(tenant_id)
        if known:
            return known
        answer = await self._call(tenant_id, self._api.profile)
        address = one_line(answer.get("emailAddress"), 320)
        if "@" not in address:
            raise ValueError("Gmail named no mailbox")
        self._mailbox[tenant_id] = address
        return address

    # --- the three actions ----------------------------------------------------

    async def _search(self, tenant_id: str, args: dict) -> dict:
        query, limit = args.get("query"), args.get("limit", SEARCH_DEFAULT)
        if (set(args) - {"query", "limit"} or not isinstance(query, str)
                or not 0 < len(query.strip()) <= MAX_QUERY_CHARS
                or not isinstance(limit, int) or isinstance(limit, bool)
                or not 1 <= limit <= SEARCH_MAX):
            return {"ok": False, "error": "bad_request"}
        mailbox = await self._address(tenant_id)
        found = await self._call(tenant_id, self._api.find, query.strip(), limit)
        listed = [item["id"] for item in found.get("messages") or []
                  if isinstance(item, dict) and isinstance(item.get("id"), str)
                  and _ID.fullmatch(item["id"])][:limit]
        gate = asyncio.Semaphore(SIDE_BY_SIDE)

        async def one(message_id: str) -> dict:
            async with gate:
                return await self._call(tenant_id, self._api.message, message_id)

        messages = []
        for message in await asyncio.gather(*(one(message_id) for message_id in listed)):
            labels = message.get("labelIds") or []
            messages.append({
                "thread_id": one_line(message.get("threadId"), 64),
                "when": _when(message),
                "from": one_line(_header(message, "From")),
                "to": one_line(_header(message, "To")),
                "subject": one_line(_header(message, "Subject")),
                "snippet": one_line(message.get("snippet"), 300),
                "unread": "UNREAD" in labels})
        return {"ok": True, "mailbox": mailbox, "messages": messages,
                "more": bool(found.get("nextPageToken"))}

    async def _thread(self, tenant_id: str, args: dict, fields: set[str]) -> list[dict] | None:
        """The mails of the thread `args` names, oldest first, or None when
        the arguments are not a thread id and these fields."""
        thread_id = args.get("thread_id")
        if set(args) - fields or not isinstance(thread_id, str) or not _ID.fullmatch(thread_id):
            return None
        answer = await self._call(tenant_id, self._api.thread, thread_id)
        messages = [m for m in answer.get("messages") or [] if isinstance(m, dict)]
        if not messages:
            raise _Refused(404)
        return messages

    async def _read(self, tenant_id: str, args: dict) -> dict:
        mailbox = await self._address(tenant_id)
        messages = await self._thread(tenant_id, args, {"thread_id"})
        if messages is None:
            return {"ok": False, "error": "bad_request"}
        kept = messages[-MAX_THREAD_MESSAGES:]
        read = []
        for message in kept:
            body, cut = body_of(message.get("payload"))
            read.append({"when": _when(message),
                         "from": one_line(_header(message, "From")),
                         "to": one_line(_header(message, "To")),
                         "cc": one_line(_header(message, "Cc")),
                         "body": body, "cut": cut})
        return {"ok": True, "mailbox": mailbox, "thread_id": args["thread_id"],
                "subject": one_line(_header(messages[0], "Subject")),
                "messages": read, "left_out": len(messages) - len(kept)}

    async def _draft(self, tenant_id: str, args: dict) -> dict:
        body = args.get("body")
        if not isinstance(body, str) or not 0 < len(body.strip()) <= MAX_DRAFT_CHARS:
            return {"ok": False, "error": "bad_request"}
        mailbox = await self._address(tenant_id)
        messages = await self._thread(tenant_id, args, {"thread_id", "body"})
        if messages is None:
            return {"ok": False, "error": "bad_request"}
        reply = reply_message(messages, mailbox, _CONTROL.sub(" ", body.replace("\r\n", "\n")))
        if reply is None:
            return {"ok": False, "error": "no_recipient"}
        made = await self._call(tenant_id, self._api.create_draft, args["thread_id"], reply)
        _LOG.info("tenant=%s: a draft was saved", tenant_id)
        return {"ok": True, "mailbox": mailbox, "thread_id": args["thread_id"],
                "draft_id": one_line(made.get("id"), 64),
                "to": ", ".join(address for _, address in reply_to(messages, mailbox)),
                "subject": one_line(_header(messages[0], "Subject"))}
