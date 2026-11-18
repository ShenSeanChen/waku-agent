"""GitHub as an event source (spec 023): an issue that carries the label
`waku`, or a comment that mentions the agent, becomes one row in `events`.

Two ways in, and both write the same event:

  the poller (default)   Every 60 seconds `waku serve` asks the `gh` CLI for
                         open issues labelled `waku` in WAKU_GITHUB_REPOS, and
                         for new comments that mention WAKU_GITHUB_LOGIN. A
                         laptop needs no public address. `gh` keeps its own
                         credentials, so there is no GitHub token here.
  the webhook (servers)  With GITHUB_WEBHOOK_SECRET set, `waku serve` receives
                         POST /webhooks/github instead, checks GitHub's
                         X-Hub-Signature-256 on every delivery, and the poller
                         stays off. Point the repository's webhook at it with
                         the events "Issues" and "Issue comments".

Who is heard: whoever can label an issue, and for a mention, an owner, member
or collaborator of the repository. A stranger's comment on a public repository
never reaches the model.

  external_id   the webhook's X-GitHub-Delivery; `label:<repo>#<n>` and
                `comment:<id>` from the poller
  session_id    github:<owner>/<repo>#<number>, one conversation per issue
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from waku.alwayson.events import Inbox
from waku.tools.github import _REPO_RE, _run

LABEL = "waku"
POLL_SECONDS = 60
# A comment only counts from someone with a role in the repository.
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
_MAX_BODY_CHARS = 8000


def login() -> str:
    """The GitHub login a comment has to mention. Empty turns mentions off."""
    return os.getenv("WAKU_GITHUB_LOGIN", "").strip().lstrip("@")


def repos() -> list[str]:
    """WAKU_GITHUB_REPOS as owner/name values. A value that does not match is
    dropped here, before any process exists, as waku/tools/github.py does."""
    found = []
    for value in os.getenv("WAKU_GITHUB_REPOS", "").split(","):
        value = value.strip()
        if not value:
            continue
        if _REPO_RE.match(value):
            found.append(value)
        else:
            print(f"(github) ignoring '{value}' in WAKU_GITHUB_REPOS: use the owner/name form")
    return found


def _mentions(body: str, who: str) -> bool:
    if not who:
        return False
    return re.search(rf"(?<![\w@.-])@{re.escape(who)}(?![\w-])", body or "", re.IGNORECASE) is not None


def _label_event(repo: str, number: int, title: str, body: str, opener: str,
                 sender: str, url: str) -> dict:
    text = (f'GitHub issue {repo}#{number} "{title}" has the label {LABEL}. '
            f"{opener} opened it.\n\n{(body or '')[:_MAX_BODY_CHARS]}").strip()
    return {"session_id": f"github:{repo}#{number}",
            "payload": {"text": text, "sender": {"login": sender},
                        "reply_to": {"repo": repo, "number": number}, "url": url}}


def _mention_event(repo: str, number: int, title: str, body: str, author: str, url: str) -> dict:
    where = f"GitHub issue {repo}#{number}" + (f' "{title}"' if title else "")
    text = f"{author} mentioned you in a comment on {where}:\n\n{(body or '')[:_MAX_BODY_CHARS]}"
    return {"session_id": f"github:{repo}#{number}",
            "payload": {"text": text, "sender": {"login": author},
                        "reply_to": {"repo": repo, "number": number}, "url": url}}


# ---- the webhook -----------------------------------------------------------

def verified(secret: str, body: bytes, signature: str) -> bool:
    """GitHub signs each delivery with the webhook's secret. Without a secret
    nothing verifies: an empty key would sign anybody's body."""
    if not secret:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature or "", expected)


def from_webhook(event: str, data: dict, who: str) -> dict | None:
    """The event one delivery asks for, or None when it asks for nothing."""
    issue = data.get("issue") or {}
    repo = (data.get("repository") or {}).get("full_name") or ""
    number = issue.get("number")
    if not repo or not isinstance(number, int):
        return None
    title, url = issue.get("title") or "", issue.get("html_url") or ""
    if event == "issues":
        labels = {label.get("name") for label in issue.get("labels") or []}
        labelled = (data.get("action") == "labeled"
                    and (data.get("label") or {}).get("name") == LABEL)
        opened = data.get("action") == "opened" and LABEL in labels
        if not (labelled or opened):
            return None
        opener = (issue.get("user") or {}).get("login") or "someone"
        sender = (data.get("sender") or {}).get("login") or opener
        return _label_event(repo, number, title, issue.get("body") or "", opener, sender, url)
    if event == "issue_comment" and data.get("action") == "created":
        comment = data.get("comment") or {}
        author = (comment.get("user") or {}).get("login") or ""
        if (not _mentions(comment.get("body") or "", who) or author.lower() == who.lower()
                or comment.get("author_association") not in TRUSTED_ASSOCIATIONS):
            return None
        return _mention_event(repo, number, title, comment.get("body") or "", author,
                              comment.get("html_url") or url)
    return None


def webhook_route(inbox: Inbox, secret: str, who: str):
    """The handler behind POST /webhooks/github: (headers, body) -> (status, JSON)."""
    def handle(headers: Mapping[str, str], body: bytes) -> tuple[int, bytes]:
        if not verified(secret, body, headers.get("X-Hub-Signature-256", "")):
            print("(github) rejected a webhook: bad signature")
            return 403, b"{}"
        delivery = headers.get("X-GitHub-Delivery", "")
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if not delivery or not isinstance(data, dict):
            return 400, b"{}"
        found = from_webhook(headers.get("X-GitHub-Event", ""), data, who)
        if found is not None:
            inbox.add(source="github", external_id=delivery, **found)
        return 200, b"{}"

    return handle


def route(inbox: Inbox):
    """The webhook handler, or None without GITHUB_WEBHOOK_SECRET."""
    secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")
    return webhook_route(inbox, secret, login()) if secret else None


# ---- the poller ------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Poller:
    """Ask `gh` what is new, once a minute. Every argv is built here from
    literals and validated repository names; `gh` only reads."""

    def __init__(self, inbox: Inbox, repositories: list[str], who: str, *,
                 run: Callable[[list[str]], tuple[bool, str]] = _run,
                 now: Callable[[], str] = _utc_now,
                 log: Callable[[str], None] = print) -> None:
        self.inbox, self.repos, self.login = inbox, repositories, who
        self._run, self._now, self._log = run, now, log
        self._since = now()            # comments are read from here on
        self._last_error = ""
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def _gh(self, argv: list[str]) -> list | None:
        ok, text = self._run(argv)
        if ok:
            try:
                rows = json.loads(text or "[]")
            except ValueError:
                ok, text = False, "gh returned something that is not JSON"
            else:
                self._last_error = ""
                return rows if isinstance(rows, list) else []
        if text != self._last_error:    # the same failure is said once
            self._last_error = text
            self._log(f"(github) could not poll: {text}")
        return None

    def _labelled(self, repo: str) -> int:
        rows = self._gh(["gh", "issue", "list", "--repo", repo, "--label", LABEL,
                         "--state", "open", "--limit", "50",
                         "--json", "number,title,body,author,url"])
        added = 0
        for row in rows or []:
            number = row.get("number")
            if not isinstance(number, int):
                continue
            opener = (row.get("author") or {}).get("login") or "someone"
            found = _label_event(repo, number, row.get("title") or "", row.get("body") or "",
                                 opener, opener, row.get("url") or "")
            added += self.inbox.add(source="github", external_id=f"label:{repo}#{number}",
                                    **found)
        return added

    def _mentioned(self, repo: str, since: str) -> int | None:
        rows = self._gh(["gh", "api", "--method", "GET", f"repos/{repo}/issues/comments",
                         "-f", f"since={since}", "-f", "per_page=100"])
        if rows is None:
            return None
        added = 0
        for row in rows:
            author = (row.get("user") or {}).get("login") or ""
            tail = (row.get("issue_url") or "").rsplit("/", 1)[-1]
            if (not tail.isdigit() or not row.get("id")
                    or not _mentions(row.get("body") or "", self.login)
                    or author.lower() == self.login.lower()
                    or row.get("author_association") not in TRUSTED_ASSOCIATIONS):
                continue
            found = _mention_event(repo, int(tail), "", row.get("body") or "", author,
                                   row.get("html_url") or "")
            added += self.inbox.add(source="github", external_id=f"comment:{row['id']}",
                                    **found)
        return added

    def poll_once(self) -> int:
        """One pass over every repository. Returns how many events it added."""
        started, since, added, complete = self._now(), self._since, 0, True
        for repo in self.repos:
            added += self._labelled(repo)
            if self.login:
                mentioned = self._mentioned(repo, since)
                complete = complete and mentioned is not None
                added += mentioned or 0
        if complete:
            self._since = started   # a failed pass is read again next time
        return added

    def run_forever(self, wait: Callable[[float], object] | None = None) -> None:
        wait = wait or self.stop_event.wait
        while not self.stop_event.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                self._log(f"(github) a poll failed: {type(exc).__name__}")
            wait(POLL_SECONDS)

    def start(self) -> Poller:
        self.thread = threading.Thread(target=self.run_forever, name="github-poll", daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=30)


def start(inbox: Inbox) -> Poller | None:
    """Start the poller, or return None when there is nothing to poll or the
    webhook is delivering the same events."""
    repositories = repos()
    if os.getenv("GITHUB_WEBHOOK_SECRET", ""):
        if repositories:
            print("(github) GITHUB_WEBHOOK_SECRET is set, so the webhook is used and "
                  "WAKU_GITHUB_REPOS is not polled")
        return None
    if not repositories:
        return None
    who = login()
    print(f"(github) polling {', '.join(repositories)} every {POLL_SECONDS} seconds for the "
          f"label {LABEL}" + (f" and mentions of @{who}" if who else ""))
    return Poller(inbox, repositories, who).start()
