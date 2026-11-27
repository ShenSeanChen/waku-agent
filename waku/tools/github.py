"""GitHub through the `gh` CLI you already signed into: `github_read` reads,
and `github_review` drafts one review that waits for a maintainer's yes.

Waku stores no GitHub token. It shells out to `gh`, which keeps its own
credentials in the system keychain — so nothing sensitive lands in `.env`, in
`.waku/`, or in a config file somebody might commit. If you have never run
`gh auth login`, every call here says so in a sentence.

THE SAFETY RULE, and why this file is shaped the way it is:

    ARGV IS CONSTRUCTED, NEVER FILTERED.

The model does not hand us a command string that we sanitise. It hands us a
label we look up in a table of six read-only commands, plus a couple of typed
scalars we validate and then place ourselves. Everything the subprocess sees is
either a literal written in this file or a value that matched a regex. There is
no code path where model output becomes a `gh` flag.

That is a stronger guarantee than blocklisting `merge`, `close` and friends,
because a blocklist has to imagine every dangerous verb in advance and `gh` adds
subcommands every release. An allowlist only has to be right about the six we
want. Refusal happens BEFORE a process exists, which is what
test_gh_tool.py::test_write_commands_never_reach_a_subprocess pins.

Same subprocess discipline as apple.py: an explicit timeout on every call,
never `shell=True`, and failures come back as a sentence rather than an
exception, because a GitHub hiccup must not take down a chat turn.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from waku.tools.registry import Tool

_TIMEOUT = 20
# A PR diff is unbounded — waku-agent has had 400-line ones and the wider world
# has 40,000-line ones. Truncating in the TOOL rather than at each call site
# means no caller can forget to, and the model is told plainly that it happened
# instead of silently reasoning about half a diff.
_MAX_CHARS = 8000
# owner/name. Both halves must START with an alphanumeric or underscore: a
# value like `-x/y` matches the looser [\w.-]+ form, and an argv element
# beginning with a hyphen is exactly the thing that gets read as a flag. GitHub
# does not allow such names anyway, so nothing legitimate is lost.
_REPO_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*$")

# The whole allowlist. The VALUE is the fixed argv prefix; the key is the only
# thing the model gets to choose. Read-only, exhaustively — no create, comment,
# merge, close, edit, or `gh api` (which would be a write primitive wearing a
# read-only name).
_ALLOWED: dict[str, list[str]] = {
    "pr list": ["pr", "list"],
    "pr view": ["pr", "view"],
    "pr diff": ["pr", "diff"],
    # The CI GitHub already ran on a pull request (spec 035): the PR desk reads
    # it instead of running tests itself.
    "pr checks": ["pr", "checks"],
    "issue list": ["issue", "list"],
    "issue view": ["issue", "view"],
}
_NEEDS_NUMBER = {"pr view", "pr diff", "pr checks", "issue view"}
_IS_LIST = {"pr list", "issue list"}
_JSON_FIELDS = "number,title,author,updatedAt,url"
# A pull request also carries its size, which the PR desk's digest reports.
# Issues have no such fields, and gh rejects a field the type lacks.
_PR_LIST_FIELDS = _JSON_FIELDS + ",additions,deletions,changedFiles,isDraft"
_CHECK_FIELDS = "name,state,bucket,workflow"
# `pr view` also reads who reviewed the pull request and when its last commit
# came (spec 035), so the PR desk skips one it reviewed already unless new
# commits arrived after that review.
_HISTORY_FIELDS = "reviews,commits"
# The end of every review this agent posted (alwayson/senders/github.py).
_OWN_REVIEW_RE = re.compile(r"<!-- waku:[^>]*-->\s*$")
# `gh pr checks` exits 1 when a check failed and 8 when one is pending, and
# prints its JSON either way.
_OK_CODES: dict[str, tuple[int, ...]] = {"pr checks": (0, 1, 8)}

_REFUSAL = (
    "Waku's GitHub tool is read-only. Allowed commands: "
    + ", ".join(sorted(_ALLOWED))
    + ". It cannot merge, comment, close, edit or push — do those yourself."
)


def _run(argv: list[str], timeout: int = _TIMEOUT, ok_codes: tuple[int, ...] = (0,),
         input_text: str | None = None) -> tuple[bool, str]:
    """One `gh` invocation. Returns (ok, text); the text is an explanation when
    ok is False. Mirrors apple.py::_osa — same contract, different binary.
    `ok_codes` names the exit codes that still carry an answer on stdout.
    `input_text` goes to gh on standard input, so a review's text never
    becomes an argument."""
    try:
        # gh emits UTF-8 even when the Windows locale uses a legacy encoding.
        r = subprocess.run(
            argv, input=input_text, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False,
        )
    except FileNotFoundError:
        return False, ("The gh CLI is not installed. Install it (brew install gh) "
                       "and run `gh auth login`.")
    except subprocess.TimeoutExpired:
        return False, (f"gh timed out after {timeout}s — GitHub or the network is slow. "
                       "Try again, or check status.github.com.")
    except OSError as exc:
        return False, f"could not run gh ({exc})"
    if r.returncode not in ok_codes or (r.returncode != 0 and not (r.stdout or "").strip()):
        err = (r.stderr or "gh failed").strip()[:200]
        if "auth" in err.lower() or "logged in" in err.lower():
            return False, f"gh is not authenticated. Run `gh auth login`. ({err})"
        return False, err
    return True, r.stdout.strip()


def _argv(command: str, number: int, repo: str, limit: int) -> tuple[bool, list[str] | str]:
    """Validate, then BUILD. Returns (ok, argv) or (False, refusal sentence).

    Every rejection here happens before a subprocess exists. `repo` is matched
    against a regex rather than escaped, so a hostile value like
    `owner/repo; rm -rf ~` is refused outright instead of being passed along in
    a form we hope is safe.
    """
    if command not in _ALLOWED:
        return False, _REFUSAL
    if repo and not _REPO_RE.match(repo):
        return False, f"'{repo}' is not a valid repo — use the owner/name form, e.g. octocat/Hello-World."
    argv = ["gh", *_ALLOWED[command]]
    if command in _NEEDS_NUMBER:
        try:
            n = int(number)
        except (TypeError, ValueError):
            return False, f"'{command}' needs a PR/issue number."
        if n <= 0:
            return False, f"'{command}' needs a positive PR/issue number."
        argv.append(str(n))
    if repo:
        argv += ["--repo", repo]
    if command in _IS_LIST:
        try:
            row_limit = int(limit or 10)
        except (TypeError, ValueError, OverflowError):
            return False, f"'{command}' needs an integer limit."
        argv += ["--limit", str(max(1, min(row_limit, 50)))]
        argv += ["--json", _PR_LIST_FIELDS if command == "pr list" else _JSON_FIELDS]
    if command == "pr checks":
        argv += ["--json", _CHECK_FIELDS]
    return True, argv


def _rows(command: str, repo: str, limit: int) -> tuple[bool, list[dict] | str]:
    """A list command as structured rows. The gather workflow routes on counts,
    so it needs data; the model needs prose. Both come from this one call so
    they can never disagree about what is open."""
    ok, argv = _argv(command, 0, repo, limit)
    if not ok:
        return False, argv  # type: ignore[return-value]
    ok, out = _run(argv)  # type: ignore[arg-type]
    if not ok:
        return False, out
    try:
        rows = json.loads(out or "[]")
    except json.JSONDecodeError:
        return False, f"gh returned something that was not JSON: {out[:200]}"
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return False, "gh did not return a JSON list of objects."
    return True, rows


def _format(rows: list[dict]) -> str:
    if not rows:
        return "(none open)"
    lines = []
    for r in rows:
        who = (r.get("author") or {}).get("login", "?")
        size = (f" · +{r['additions']}/-{r['deletions']} in {r.get('changedFiles', '?')} files"
                if "additions" in r and "deletions" in r else "")
        draft = " · draft" if r.get("isDraft") else ""
        lines.append(f"#{r.get('number')} {r.get('title', '')} — @{who} "
                     f"(updated {(r.get('updatedAt') or '')[:10]}){size}{draft}")
    return "\n".join(lines)


def _checks(out: str) -> str:
    """`gh pr checks --json` as one summary line and one line per check."""
    try:
        rows = json.loads(out or "[]")
    except json.JSONDecodeError:
        return f"gh returned something that was not JSON: {out[:200]}"
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return "gh did not return a JSON list of objects."
    if not rows:
        return "No CI checks are reported on this pull request."
    count = {b: sum(1 for row in rows if row.get("bucket") == b) for b in ("pass", "fail", "pending")}
    lines = [f"checks: {count['pass']} pass, {count['fail']} fail, {count['pending']} pending"]
    for row in rows:
        workflow = f"{row['workflow']} / " if row.get("workflow") else ""
        lines.append(f"{row.get('bucket', '?')}: {workflow}{row.get('name', '')}")
    return "\n".join(lines)


def _history(argv: list[str]) -> str:
    """Who reviewed a pull request and when its last commit came, as lines to
    put under `pr view`, or "" when gh could not say."""
    ok, out = _run([*argv, "--json", _HISTORY_FIELDS])
    try:
        data = json.loads(out) if ok else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        return ""
    reviews = [r for r in data.get("reviews") or [] if isinstance(r, dict)]
    lines = ["reviews:" if reviews else "reviews: none yet"]
    for review in reviews:
        own = (" (drafted by this agent)"
               if _OWN_REVIEW_RE.search(str(review.get("body") or "")) else "")
        lines.append(f"  {(review.get('author') or {}).get('login', '?')} "
                     f"{review.get('state', '?')} {review.get('submittedAt', '?')}{own}")
    commits = [c for c in data.get("commits") or [] if isinstance(c, dict)]
    if commits:
        lines.append(f"last commit: {commits[-1].get('committedDate', '?')}")
    return "\n".join(lines)


def list_prs(repo: str = "", limit: int = 10) -> tuple[bool, list[dict] | str]:
    """Open PRs as rows. Used directly by the gather workflow — no registry, no
    model in the loop, which is why gather works with the tool switched off."""
    return _rows("pr list", repo, limit)


def list_issues(repo: str = "", limit: int = 10) -> tuple[bool, list[dict] | str]:
    """Open issues as rows. Same contract as list_prs."""
    return _rows("issue list", repo, limit)


def gh_read(command: str, number: int = 0, repo: str = "", limit: int = 10) -> str:
    """The single entry point the model reaches. Always returns text."""
    if command in _IS_LIST:
        ok, res = _rows(command, repo, limit)
        return _format(res) if ok else str(res)  # type: ignore[arg-type]
    ok, argv = _argv(command, number, repo, limit)
    if not ok:
        return str(argv)
    codes = _OK_CODES.get(command)
    # Only `pr checks` passes exit codes, so a caller that stands in its own
    # `_run(argv)` (the always-on example eval) still works for every other row.
    ok, out = (_run(argv, ok_codes=codes) if codes else _run(argv))  # type: ignore[arg-type]
    if not ok:
        return out
    if command == "pr checks":
        return _checks(out)
    if len(out) > _MAX_CHARS:
        out = out[:_MAX_CHARS] + (
            f"\n\n… truncated at {_MAX_CHARS} chars. Run `gh {command} {number}` "
            "in a terminal to read the rest."
        )
    if command == "pr view" and (history := _history(argv)):  # type: ignore[arg-type]
        out += "\n\n" + history
    return out


def make_tool(default_repo: str = "") -> Tool:
    def run(command: str, number: int = 0, repo: str = "", limit: int = 10) -> str:
        return gh_read(command, number, repo or default_repo, limit)

    return Tool(
        name="github_read",
        description=(
            "Read pull requests and issues from GitHub via the gh CLI. "
            "command must be one of: " + ", ".join(sorted(_ALLOWED)) + ". "
            "READ-ONLY — it cannot merge, comment, close, edit or push, and will "
            "refuse if asked to. Use it to review what is open and what changed."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "one of: " + ", ".join(sorted(_ALLOWED))},
                "number": {"type": "integer", "description": "PR/issue number (required for view and diff)"},
                "repo": {"type": "string", "description": "owner/name; omit to use the current repo"},
                "limit": {"type": "integer", "description": "max rows for list commands, default 10"},
            },
            "required": ["command"],
        },
        fn=run,
    )


# ---- github_review: the one write, and it always waits (spec 035) ----------
#
# This tool never runs `gh`. Under the always-on mode the approval gate stores the
# call, and a maintainer approves the exact text with a button in Slack
# (waku/alwayson/approvals.py). Only then does the tool run, and it queues one
# row that the GitHub sender posts with `gh pr review <n> --comment
# --body-file -`. A review is always a comment: an approval from the agent
# would count toward branch protection on a repository that requires one.

REVIEW_MAX_CHARS = 65000


def make_review_tool(home: Path) -> Tool:
    def github_review(repo: str, number: int, body: str) -> str:
        from waku.alwayson import outbox
        from waku.alwayson.sources.github import repos
        from waku.db import connect

        repo = (repo or "").strip() if isinstance(repo, str) else ""
        if repo not in repos():
            return (f"Not posted: {repo or 'that repository'} is not in WAKU_GITHUB_REPOS, "
                    "so Waku does not review it.")
        try:
            n = int(number)
        except (TypeError, ValueError):
            return "Not posted: `number` is the pull request's number."
        if n <= 0:
            return "Not posted: `number` is the pull request's number."
        if not isinstance(body, str) or not body.strip():
            return "Not posted: the review is empty."
        if len(body) > REVIEW_MAX_CHARS:
            return f"Not posted: a review is at most {REVIEW_MAX_CHARS} characters."
        run = outbox.current_run(Path(home))
        if run is None or run.approved_key is None:
            return ("Not posted: a review is posted only after a maintainer approves its "
                    "exact text, and this chat cannot ask for that.")
        conn = connect(Path(home))
        try:
            written = outbox.insert(conn, run_id=run.run_id, channel="github",
                                    target={"repo": repo, "number": n, "review": True},
                                    body=body, idempotency_key=run.next_key())
        finally:
            conn.close()
        if written is None:
            return f"The review on {repo}#{n} was already queued by an earlier attempt."
        return f"The review on {repo}#{n} is on its way, as a comment."

    return Tool(
        name="github_review",
        description=(
            "Draft a review of one pull request in a repository Waku watches. The review "
            "is posted on GitHub as a comment, never as an approval or a request for "
            "changes, and only after a maintainer approves its exact text. Use it in the "
            "PR desk's morning run for the one pull request that most needs a reviewer."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/name"},
                "number": {"type": "integer", "description": "the pull request's number"},
                "body": {"type": "string",
                         "description": "The review exactly as it should be posted"},
            },
            "required": ["repo", "number", "body"],
        },
        fn=github_review,
    )
