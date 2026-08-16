"""DETERMINISTIC EVAL — the delegate env denylist actually reaches the child.

A denylist that has quietly stopped working looks EXACTLY like one that works:
the call still succeeds, the child still runs, nothing logs a warning. The only
honest test is to spawn a real process and ask it what it can see.

So `test_autorun_child_cannot_read_a_denied_var` has a twin,
`..._control_the_child_normally_can_`, which asserts the child DOES read the
secret with the policy off. Without that pair, a helper that returned an empty
env — or one that stripped nothing at all — would pass just as happily.

The other half of the file guards the wiring: every place that spawns pi, or
runs code pi wrote, must pass env=delegate_env(). That is the part most likely
to rot, because adding a new spawn point is a one-line change and forgetting
the keyword is invisible at review.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from waku.tools._env import delegate_env, delegate_env_denylist

DENY = "WAKU_DELEGATE_ENV_DENY"


@pytest.fixture(autouse=True)
def clean_policy(monkeypatch):
    monkeypatch.delenv(DENY, raising=False)


# --------------------------------------------------------------- the helper


def test_no_policy_strips_nothing(monkeypatch):
    """Opt-in: with nothing configured, the delegate sees what it always saw."""
    monkeypatch.setenv("SOME_KEY", "value")
    assert delegate_env()["SOME_KEY"] == "value"


def test_policy_set_after_import_is_honoured(monkeypatch):
    """Read at CALL time, not import time — a host app configures the policy
    after `import waku`, and a module-level constant would miss it."""
    monkeypatch.setenv("SECRET", "hunter2")
    monkeypatch.setenv(DENY, "SECRET")
    env = delegate_env()
    assert "SECRET" not in env


def test_unnamed_vars_survive(monkeypatch):
    monkeypatch.setenv("SECRET", "hunter2")
    monkeypatch.setenv("KEEP", "fine")
    monkeypatch.setenv(DENY, "SECRET")
    assert delegate_env()["KEEP"] == "fine"


def test_the_policy_var_itself_is_never_stripped(monkeypatch):
    """A nested waku inside the delegate must inherit the same policy. Strip the
    denylist itself and the second hop silently runs wide open."""
    monkeypatch.setenv("SECRET", "hunter2")
    monkeypatch.setenv(DENY, "SECRET")
    assert delegate_env()[DENY] == "SECRET"


def test_list_tolerates_spaces_and_empty_entries(monkeypatch):
    monkeypatch.setenv("A1", "x")
    monkeypatch.setenv("B2", "y")
    monkeypatch.setenv(DENY, " A1 , ,B2 ")
    assert delegate_env_denylist() == frozenset({"A1", "B2"})
    env = delegate_env()
    assert "A1" not in env and "B2" not in env


def test_naming_a_var_that_is_not_set_is_not_an_error(monkeypatch):
    monkeypatch.setenv(DENY, "NEVER_SET_ANYWHERE")
    delegate_env()   # must not raise


# ------------------------------------------------- a REAL child process


def _child_that_reports(tmp_path, var):
    d = tmp_path / "run"
    d.mkdir()
    (d / "main.py").write_text(
        f"import os\nprint('SAW=' + os.environ.get({var!r}, '<stripped>'))\n",
        encoding="utf-8")
    return d


def test_autorun_child_cannot_read_a_denied_var(tmp_path, monkeypatch):
    """The end-to-end claim: code the delegate wrote, executed by waku, cannot
    read a var the host denied."""
    from waku.tools import workspace

    monkeypatch.setenv("MY_SECRET", "hunter2")
    monkeypatch.setenv(DENY, "MY_SECRET")
    result = workspace.autorun(_child_that_reports(tmp_path, "MY_SECRET"))
    assert result is not None
    assert "SAW=<stripped>" in result[2]


def test_control_the_child_normally_can_read_it(tmp_path, monkeypatch):
    """The twin that makes the test above mean something. Delete the env= on
    autorun's Popen and THIS one keeps passing while the one above fails —
    which is exactly the signal we want."""
    from waku.tools import workspace

    monkeypatch.setenv("MY_SECRET", "hunter2")
    result = workspace.autorun(_child_that_reports(tmp_path, "MY_SECRET"))
    assert result is not None
    assert "SAW=hunter2" in result[2]


# ------------------------------------------------------------- the wiring

# Every spawn that runs pi, or runs code pi wrote. The `pi --help` probe in
# _pi_supports_json is deliberately absent: it prints usage and exits without
# executing anything the model chose.
DELEGATE_SPAWNS = 7
SPAWN_FILES = ["waku/tools/experimental.py",
               "waku/tools/workspace.py",
               "waku/ops/coding_eval.py"]


def _spawns(path):
    """Yield every subprocess.run/Popen call node in a file."""
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if (isinstance(fn, ast.Attribute) and fn.attr in ("run", "Popen")
                and isinstance(fn.value, ast.Name) and fn.value.id == "subprocess"):
            yield node


def _passes_delegate_env(call):
    # experimental.py imports it as `delegate_env as _delegate_env`, so match on
    # the suffix rather than pinning one spelling — the alias is not the point.
    return any(kw.arg == "env"
               and isinstance(kw.value, ast.Call)
               and getattr(kw.value.func, "id", "").endswith("delegate_env")
               for kw in call.keywords)


def test_every_delegate_spawn_passes_delegate_env():
    """Adding a spawn point is one line; forgetting env= is invisible at review.
    This counts them instead of trusting a reviewer's eye."""
    wired = sum(1 for f in SPAWN_FILES for c in _spawns(f) if _passes_delegate_env(c))
    assert wired == DELEGATE_SPAWNS, (
        f"{wired} of {DELEGATE_SPAWNS} delegate spawn points pass env=delegate_env(). "
        "A new one was added without it, or an existing one lost it.")


def test_no_delegate_spawn_leaks_a_raw_environ_copy():
    """`env=os.environ.copy()` is the exact anti-pattern delegate_env replaces —
    it looks deliberate, so it survives review, and it hands over everything."""
    for f in SPAWN_FILES:
        for call in _spawns(f):
            for kw in call.keywords:
                if kw.arg != "env":
                    continue
                src = ast.unparse(kw.value)
                assert "environ" not in src, f"{f}: spawn passes env={src}"
