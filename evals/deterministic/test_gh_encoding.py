"""The GitHub tool decodes real subprocess pipes independently of the locale."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from waku.tools import github


@pytest.fixture
def gh_process(monkeypatch, tmp_path):
    """Use real pipes with cp1252 as the parent's implicit text encoding.

    A local Python child stands in for gh; it writes bytes without consulting
    the console encoding. Explicit subprocess encodings still take precedence.
    """
    result = {"stdout": b"", "stderr": b"", "returncode": 0}
    child = tmp_path / "gh_stub.py"
    run = subprocess.run

    def invoke(argv, **kwargs):
        assert argv[0] == "gh"
        child.write_text(
            "import sys\n"
            f"sys.stdout.buffer.write({result['stdout']!r})\n"
            f"sys.stderr.buffer.write({result['stderr']!r})\n"
            f"sys.exit({result['returncode']})\n",
            encoding="utf-8",
        )
        return run([sys.executable, str(child)], **kwargs)

    monkeypatch.setattr(subprocess, "_text_encoding", lambda: "cp1252")
    monkeypatch.setattr(github.subprocess, "run", invoke)
    return result


@pytest.mark.parametrize("command", ["pr list", "issue list", "pr view", "issue view", "pr diff"])
def test_gh_output_preserves_utf8_on_a_legacy_locale(command, gh_process):
    title = "Fix \u4e2d\u6587 \u00b7 \u8bb0\u5fc6"
    payload = json.dumps([{"number": 42, "title": title}], ensure_ascii=False) \
        if command.endswith("list") else title
    gh_process["stdout"] = payload.encode("utf-8")

    out = github.gh_read(command, number=42)

    assert title in out


@pytest.mark.parametrize("helper", [github.list_prs, github.list_issues])
def test_gh_list_rows_preserve_utf8_on_a_legacy_locale(helper, gh_process):
    rows = [{"number": 42, "title": "Fix \u4e2d\u6587 \u00b7 \u8bb0\u5fc6"}]
    gh_process["stdout"] = json.dumps(rows, ensure_ascii=False).encode("utf-8")

    assert helper() == (True, rows)


def test_gh_error_preserves_utf8_on_a_legacy_locale(gh_process):
    gh_process.update(stderr="\u65e0\u6cd5\u8bfb\u53d6 issue".encode("utf-8"), returncode=1)

    assert github.gh_read("issue view", number=42) == "\u65e0\u6cd5\u8bfb\u53d6 issue"


@pytest.mark.parametrize("stream,returncode,ok", [("stdout", 0, True), ("stderr", 1, False)])
def test_gh_invalid_utf8_returns_readable_text(gh_process, stream, returncode, ok):
    gh_process.update({stream: b"before \xff after", "returncode": returncode})

    assert github._run(["gh", "pr", "diff", "42"]) == (ok, "before \ufffd after")
