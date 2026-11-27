"""DETERMINISTIC EVAL — the gh tool's read-only boundary, proven offline.

CI has no `gh` and no GitHub credentials, so nothing here asserts "reading
really works" — that is verified by hand against a live repo. What CAN be
pinned in CI is the part that would actually hurt if it broke: that a model
cannot get a WRITE command past this tool, and that every failure comes back as
a sentence rather than an exception.

The design being defended, from the module docstring:

    ARGV IS CONSTRUCTED, NEVER FILTERED.

The model picks a label from a table of six; every other argv element is
either a literal in the source or a value that matched a regex. So the tests
below check the boundary in two independent ways — the refusal text, and the
stronger claim that the subprocess is never reached at all. The second is what
matters: a tool that spawns `gh pr merge` and then inspects the result has
already merged the PR.

Lesson carried over from test_apple_tools.py, where four tools sat broken for
weeks behind a green suite: assert on the command that WOULD run, not on prose
describing it.
"""

from __future__ import annotations

import inspect
import json
import subprocess

import pytest

from waku.tools import github


class Boom(Exception):
    """Raised by the fake subprocess. Reaching it at all is the failure."""


@pytest.fixture
def no_subprocess(monkeypatch):
    """Make any subprocess call explode. A test that passes with this installed
    has proven the code path never spawned a process."""
    def explode(*a, **k):
        raise Boom(f"a subprocess was spawned: {a!r}")

    monkeypatch.setattr(github.subprocess, "run", explode)


@pytest.fixture
def capture(monkeypatch):
    """Record argv and return a canned result instead of running gh."""
    seen: dict = {}

    def fake(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout=seen.get("stdout", "[]"), stderr="")

    monkeypatch.setattr(github.subprocess, "run", fake)
    return seen


# --- the boundary -----------------------------------------------------------

@pytest.mark.parametrize("command", [
    "pr merge", "pr create", "pr close", "pr comment", "pr edit",
    "issue close", "issue create", "issue comment",
    "repo delete", "api", "auth token", "", "pr", "PR LIST",
])
def test_write_commands_never_reach_a_subprocess(command, no_subprocess):
    """THE test. Refusal must happen before a process exists — not after, and
    not by inspecting what gh did. `gh api` and `auth token` are in the list on
    purpose: both read innocuous but are general-purpose escape hatches."""
    out = github.gh_read(command, number=1)
    assert "read-only" in out.lower(), out
    assert "pr list" in out, "the refusal should name what IS allowed"


def test_the_allowlist_is_exactly_the_six_read_commands():
    """Adding a seventh entry should be a deliberate, reviewed act. If this
    fails, someone widened the tool's reach — check that they meant to."""
    assert set(github._ALLOWED) == {
        "pr list", "pr view", "pr diff", "pr checks", "issue list", "issue view",
    }
    for argv in github._ALLOWED.values():
        assert not ({"merge", "close", "create", "comment", "edit", "delete", "review"}
                    & set(argv))


def test_a_hostile_repo_is_refused_not_escaped(no_subprocess):
    """Shell metacharacters are moot (there is no shell), but a bad repo value
    could still become a stray gh argument. Match a regex, refuse otherwise."""
    for bad in ("owner/repo; rm -rf ~", "--repo=x", "owner", "a/b/c", "$(whoami)/x", "-x/y"):
        out = github.gh_read("pr list", repo=bad)
        assert "not a valid repo" in out, (bad, out)


def test_view_and_diff_require_a_real_number(no_subprocess):
    for command in ("pr view", "pr diff", "pr checks", "issue view"):
        assert "number" in github.gh_read(command, number=0)
        assert "number" in github.gh_read(command, number=-3)
        assert "number" in github.gh_read(command, number="not-a-number")  # type: ignore[arg-type]


# --- argv construction ------------------------------------------------------

def test_argv_is_built_from_literals(capture):
    github.gh_read("pr view", number=42, repo="ShenSeanChen/waku-agent")
    argv = capture["argv"]
    assert argv[:3] == ["gh", "pr", "view"]
    assert argv[3] == "42", "the number must be placed by us, as its own element"
    assert argv[4:6] == ["--repo", "ShenSeanChen/waku-agent"]


def test_list_commands_ask_for_json_and_clamp_the_limit(capture):
    github.gh_read("pr list", limit=9999)
    argv = capture["argv"]
    assert "--json" in argv, "list output is parsed, so it must be requested as JSON"
    assert argv[argv.index("--limit") + 1] == "50", "an unbounded limit must be clamped"
    # 0 means "didn't say", not "give me zero rows" — a tool that honoured a
    # literal 0 would return nothing and look broken rather than unconfigured.
    github.gh_read("issue list", limit=0)
    assert capture["argv"][capture["argv"].index("--limit") + 1] == "10"


@pytest.mark.parametrize("limit", ["many", [5], float("inf")])
def test_invalid_list_limits_return_an_error_without_running_gh(limit, no_subprocess):
    assert "integer limit" in github.gh_read("pr list", limit=limit)
    ok, error = github.list_issues(limit=limit)
    assert not ok
    assert "integer limit" in error


def _code_lines(module) -> list[str]:
    """Source with docstrings and comments removed.

    github.py DISCUSSES `shell=True` at length on purpose — the docstring is
    where the rule is explained. A naive substring search over the raw source
    therefore trips over the very sentence forbidding the thing, which is
    exactly the trap test_apple_tools.py hit with its `whose` rule. Assert on
    the CODE, not on the prose about the code.
    """
    import ast

    src = inspect.getsource(module)
    doc_spans: set[int] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            doc_spans.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return [ln for i, ln in enumerate(src.splitlines(), 1)
            if i not in doc_spans and not ln.strip().startswith("#")]


def test_no_shell_true_anywhere():
    """`shell=True` would turn every argument back into a string the shell
    parses, undoing the entire design of this file."""
    offenders = [ln.strip() for ln in _code_lines(github) if "shell=True" in ln]
    assert offenders == [], offenders


def test_every_gh_call_is_bounded(capture):
    """One hung network call must not hang a chat turn — same rule as apple.py."""
    sig = inspect.signature(github._run)
    assert sig.parameters["timeout"].default, "_run must always have a timeout default"
    github.gh_read("pr list")
    assert capture["kwargs"].get("timeout"), "the timeout must reach subprocess.run"


# --- honest failure ---------------------------------------------------------

@pytest.mark.parametrize("exc,expect", [
    (FileNotFoundError(), "not installed"),
    (subprocess.TimeoutExpired(cmd="gh", timeout=20), "timed out"),
])
def test_failures_return_a_sentence_and_never_raise(monkeypatch, exc, expect):
    def raiser(*a, **k):
        raise exc

    monkeypatch.setattr(github.subprocess, "run", raiser)
    out = github.gh_read("pr list")
    assert expect in out, out


def test_an_unauthenticated_gh_says_how_to_fix_it(monkeypatch):
    """The most likely first-run failure, and the one worth naming precisely:
    'gh failed' would send someone hunting for a bug that is really a login."""
    monkeypatch.setattr(github.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 1, stdout="", stderr="gh auth login required to use this command"))
    out = github.gh_read("pr list")
    assert "gh auth login" in out


def test_a_huge_diff_is_truncated_by_the_tool(monkeypatch):
    """Truncation lives in the tool so no caller can forget it, and the model is
    told it happened rather than silently reasoning about half a diff."""
    monkeypatch.setattr(github.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout="x" * 200_000, stderr=""))
    out = github.gh_read("pr diff", number=1)
    assert len(out) < github._MAX_CHARS + 300
    assert "truncated" in out


def test_list_helpers_return_rows_for_the_router(monkeypatch):
    """The gather workflow routes on COUNTS, so the list helpers must hand back
    data, not prose. Same underlying call as the model's text view, so the two
    can never disagree about what is open."""
    payload = '[{"number":42,"title":"Fix the thing","author":{"login":"octocat"},' \
              '"updatedAt":"2026-07-31T10:00:00Z","url":"http://x"}]'
    monkeypatch.setattr(github.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout=payload, stderr=""))
    ok, rows = github.list_prs("ShenSeanChen/waku-agent")
    assert ok and isinstance(rows, list) and rows[0]["number"] == 42
    assert "#42 Fix the thing — @octocat" in github.gh_read("pr list")


def test_malformed_json_does_not_raise(monkeypatch):
    monkeypatch.setattr(github.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout="<html>rate limited</html>", stderr=""))
    ok, res = github.list_prs()
    assert ok is False
    assert "not JSON" in str(res)


@pytest.mark.parametrize("payload", ['{"error": "unavailable"}', "null", "[1]"])
def test_list_results_require_a_list_of_objects(payload, capture):
    capture["stdout"] = payload

    ok, error = github.list_prs()
    assert not ok
    assert "JSON list of objects" in error
    assert github.gh_read("issue list") == error


# --- registration -----------------------------------------------------------

def test_the_tool_is_off_unless_asked_for(tmp_path):
    """Every registered tool ships in every prompt, so a maintainer-only tool
    must not appear for people who never asked for it (the footprint
    ladder). The gather workflow imports this module directly and works with
    the switch off — the switch only decides whether the MODEL can reach it."""
    from evals.helpers import ScriptedClient, make_waku

    off = make_waku(tmp_path / "off", client=ScriptedClient([]), gh_tool=False)
    assert "github_read" not in off.tools._tools
    on = make_waku(tmp_path / "on", client=ScriptedClient([]), gh_tool=True)
    assert "github_read" in on.tools._tools


def test_the_description_tells_the_model_the_boundary():
    """The model should not have to discover the refusal by trying to merge."""
    desc = github.make_tool().description.lower()
    assert "read-only" in desc
    for verb in ("merge", "comment", "close"):
        assert verb in desc


# --- pr checks (spec 035) ---------------------------------------------------

_CHECKS = json.dumps([
    {"name": "skills-and-evals", "state": "SUCCESS", "bucket": "pass", "workflow": "validate"},
    {"name": "hosted-docker", "state": "FAILURE", "bucket": "fail", "workflow": "hosted"},
    {"name": "lint", "state": "PENDING", "bucket": "pending", "workflow": ""}])


def test_github_read_pr_checks(capture):
    """The digest reads the CI that GitHub already ran: one more read row,
    and the write guard above still refuses everything else."""
    capture["stdout"] = _CHECKS
    out = github.gh_read("pr checks", number=428, repo="acme/app")
    assert capture["argv"] == ["gh", "pr", "checks", "428", "--repo", "acme/app",
                               "--json", "name,state,bucket,workflow"]
    assert out == ("checks: 1 pass, 1 fail, 1 pending\n"
                   "pass: validate / skills-and-evals\n"
                   "fail: hosted / hosted-docker\n"
                   "pending: lint")


@pytest.mark.parametrize("code", [1, 8])
def test_pr_checks_reads_json_when_gh_exits_for_a_red_or_pending_check(monkeypatch, code):
    """`gh pr checks` exits 1 when a check failed and 8 when one is pending,
    and prints the JSON all the same. A red CI is an answer, not an error."""
    monkeypatch.setattr(github.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], code, stdout=_CHECKS, stderr=""))
    assert github.gh_read("pr checks", number=428).startswith("checks: 1 pass, 1 fail")


def test_pr_checks_with_no_checks_says_so(monkeypatch):
    monkeypatch.setattr(github.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 1, stdout="", stderr="no checks reported on the 'feature' branch"))
    assert github.gh_read("pr checks", number=428) == "no checks reported on the 'feature' branch"


def test_another_command_still_fails_on_a_non_zero_exit(monkeypatch):
    monkeypatch.setattr(github.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 1, stdout="[]", stderr="HTTP 404"))
    assert github.gh_read("pr view", number=428) == "HTTP 404"


def test_pr_list_reports_each_pull_requests_size(capture):
    """The PR desk's digest gives a size per pull request. Before this, pr list
    asked gh for no size, and the digest left sizes out on the live test
    (2026-11-25). Issues have no size, and gh rejects a field the type lacks."""
    capture["stdout"] = json.dumps([{
        "number": 428, "title": "check full fact table for duplicates",
        "author": {"login": "someone"}, "updatedAt": "2026-11-24T10:00:00Z",
        "url": "u", "additions": 140, "deletions": 15, "changedFiles": 3,
        "isDraft": False}])
    out = github.gh_read("pr list", repo="acme/app")
    fields = capture["argv"][capture["argv"].index("--json") + 1].split(",")
    assert {"additions", "deletions", "changedFiles"} <= set(fields)
    assert "+140/-15 in 3 files" in out

    github.gh_read("issue list", repo="acme/app")
    fields = capture["argv"][capture["argv"].index("--json") + 1].split(",")
    assert "additions" not in fields


def test_a_green_checks_summary_does_not_read_as_a_tool_error(capture):
    """The dashboard marks a tool call red when its output says "failed". The
    first summary read "2 passed, 0 failed", so every green pr checks call
    showed as an error on the live test (2026-11-25)."""
    from waku.ops.dashboard import _tool_status

    capture["stdout"] = _CHECKS
    assert _tool_status(github.gh_read("pr checks", number=428, repo="acme/app")) == "ok"


# --- pr view says who reviewed it (spec 035) ---------------------------------

_HISTORY = json.dumps({
    "reviews": [{"author": {"login": "wakuagent"}, "state": "COMMENTED",
                 "submittedAt": "2026-11-25T17:02:11Z",
                 "body": "Looks fine.\n\n<!-- waku:12:abcdef -->"},
                {"author": {"login": "ana"}, "state": "APPROVED",
                 "submittedAt": "2026-11-25T18:00:00Z", "body": "LGTM"}],
    "commits": [{"committedDate": "2026-11-20T09:00:00Z"},
                {"committedDate": "2026-11-24T10:00:00Z"}]})


def _view(monkeypatch, history, history_code=0):
    calls = []

    def fake(argv, **kwargs):
        calls.append(argv)
        if "--json" in argv:
            return subprocess.CompletedProcess(argv, history_code, stdout=history, stderr="nope")
        return subprocess.CompletedProcess(argv, 0, stdout="title:\tFix login\n\nThe body.",
                                           stderr="")

    monkeypatch.setattr(github.subprocess, "run", fake)
    return github.gh_read("pr view", number=1, repo="acme/app"), calls


def test_pr_view_says_who_reviewed_it_and_when_the_last_commit_came(monkeypatch):
    """Found in the live check (2026-11-26): the desk drafted a second review
    of PR #1, which wakuagent had reviewed already, because `pr view` did not
    show reviews. It now ends with each review and the last commit's date."""
    out, calls = _view(monkeypatch, _HISTORY)
    assert out.startswith("title:\tFix login\n\nThe body.")
    assert out.endswith(
        "\n\nreviews:\n"
        "  wakuagent COMMENTED 2026-11-25T17:02:11Z (drafted by this agent)\n"
        "  ana APPROVED 2026-11-25T18:00:00Z\n"
        "last commit: 2026-11-24T10:00:00Z")
    assert calls[1] == ["gh", "pr", "view", "1", "--repo", "acme/app",
                        "--json", "reviews,commits"]


def test_pr_view_says_when_nobody_reviewed_it(monkeypatch):
    out, _ = _view(monkeypatch, json.dumps({"reviews": [], "commits": []}))
    assert out.endswith("\n\nreviews: none yet")


@pytest.mark.parametrize("history, code", [("not json", 0), ("[]", 0), ("", 1)])
def test_pr_view_without_its_review_history_is_still_the_view(monkeypatch, history, code):
    out, _ = _view(monkeypatch, history, code)
    assert out == "title:\tFix login\n\nThe body."
