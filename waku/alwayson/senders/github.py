"""Send one outbox row as a comment on a GitHub issue, through the `gh` CLI.

`gh` keeps its own credentials, so there is no GitHub token here. The argv is
built from literals, a repository name that matched owner/name, and a number.
The comment's text is one `--body=` argument, so no text becomes a flag.

Each comment ends with a hidden marker that carries the row's idempotency key.
Before it posts, the sender reads the issue's comments, and a comment that
already carries the key means this row reached GitHub on an earlier attempt:
nothing is posted and the row is sent. A process that dies between GitHub's
answer and the row's `sent` mark therefore leaves one comment, not two.

A row whose target says "review" is posted as a pull request review with the
event COMMENT (spec 035): its text goes to gh on standard input, and the
marker is looked for among the pull request's reviews.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from waku.alwayson.outbox import Message
from waku.alwayson.senders import SendFailed
from waku.tools.github import _REPO_RE, _run

# GitHub refuses a comment over 65,536 characters.
MAX_CHARS = 65000


def marker(idempotency_key: str) -> str:
    return f"<!-- waku:{idempotency_key} -->"


def _carries(listing: str, mark: str, key: str = "comments") -> bool:
    """Whether a comment or review in `gh ... view --json <key>` ends with `mark`."""
    try:
        items = json.loads(listing or "{}").get(key) or []
    except (ValueError, AttributeError):
        raise SendFailed("gh returned something that is not JSON") from None
    return any(str(item.get("body", "")).rstrip().endswith(mark)
               for item in items if isinstance(item, dict))


def _review(run, message: Message, repo: str, number: int) -> None:
    """Post one row as a review with the event COMMENT (spec 035, decision
    3). The argv has no way to say approve, and the text cannot add one."""
    ok, text = run(["gh", "pr", "view", str(number), "--repo", repo, "--json", "reviews"])
    if not ok:
        raise SendFailed(text)   # without the list a review could go out twice
    if _carries(text, marker(message.idempotency_key), "reviews"):
        return
    body = f"{message.body[:MAX_CHARS]}\n\n{marker(message.idempotency_key)}"
    ok, text = run(["gh", "pr", "review", str(number), "--repo", repo, "--comment",
                    "--body-file", "-"], input_text=body)
    if not ok:
        raise SendFailed(text)


def make(run: Callable[..., tuple[bool, str]] = _run):
    def send(message: Message) -> None:
        repo, number = message.target.get("repo"), message.target.get("number")
        if (not isinstance(repo, str) or not _REPO_RE.match(repo)
                or not isinstance(number, int) or number < 1):
            raise SendFailed("the target is not an issue: it needs owner/name and a number")
        if message.target.get("review"):
            _review(run, message, repo, number)
            return
        ok, text = run(["gh", "issue", "view", str(number), "--repo", repo,
                        "--json", "comments"])
        if not ok:
            raise SendFailed(text)   # without the list a comment could go out twice
        if _carries(text, marker(message.idempotency_key)):
            return
        body = f"{message.body[:MAX_CHARS]}\n\n{marker(message.idempotency_key)}"
        ok, text = run(["gh", "issue", "comment", str(number), "--repo", repo,
                        f"--body={body}"])
        if not ok:
            raise SendFailed(text)

    return send
