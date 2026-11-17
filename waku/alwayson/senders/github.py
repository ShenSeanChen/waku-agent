"""Send one outbox row as a comment on a GitHub issue, through the `gh` CLI.

`gh` keeps its own credentials, so there is no GitHub token here. The argv is
built from literals, a repository name that matched owner/name, and a number.
The comment's text is one `--body=` argument, so no text becomes a flag.

Each comment ends with a hidden marker that carries the row's idempotency key.
"""

from __future__ import annotations

from collections.abc import Callable

from waku.alwayson.outbox import Message
from waku.alwayson.senders import SendFailed
from waku.tools.github import _REPO_RE, _run

# GitHub refuses a comment over 65,536 characters.
MAX_CHARS = 65000


def marker(idempotency_key: str) -> str:
    return f"<!-- waku:{idempotency_key} -->"


def make(run: Callable[[list[str]], tuple[bool, str]] = _run):
    def send(message: Message) -> None:
        repo, number = message.target.get("repo"), message.target.get("number")
        if (not isinstance(repo, str) or not _REPO_RE.match(repo)
                or not isinstance(number, int) or number < 1):
            raise SendFailed("the target is not an issue: it needs owner/name and a number")
        body = f"{message.body[:MAX_CHARS]}\n\n{marker(message.idempotency_key)}"
        ok, text = run(["gh", "issue", "comment", str(number), "--repo", repo,
                        f"--body={body}"])
        if not ok:
            raise SendFailed(text)

    return send
