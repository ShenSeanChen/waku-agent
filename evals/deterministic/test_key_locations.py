"""The setup page says where Waku looked for a model key (spec 013).

WHY THIS EXISTS. On 2026-10-04 Sean opened the dashboard from a git worktree
that had no .env. His keys sat in the main checkout's .env, and ~/.waku/.env
held no provider key. The page said "Pick a provider and paste an API key ...
The key is written to .env on this machine" and named no place it had looked.

Each case here builds that kind of layout in a temporary folder and checks one
property: the places come in the order they win, presence is reported without
a value, a file Waku does not read is only said to exist, the gate shows only
when no place holds a key, and a pasted key goes to <home>/.env when the folder
has no .env of its own.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from waku import config, integrations
from waku.key_locations import cli_lines, model_key_locations

ROOT = Path(__file__).resolve().parents[2]
SETUP = ROOT / "waku" / "ops" / "static" / "js" / "setup.js"
SECRET = "sk-ant-never-shown-0123456789"


def _seans_layout(tmp_path: Path) -> SimpleNamespace:
    """A worktree with no .env, a ~/.waku/.env without a provider key, and a
    main checkout whose .env holds one: Sean's machine on 2026-10-04."""
    user_home = tmp_path / "u"
    harness = user_home / "Developer" / "waku-agent-harness"
    main, worktree = harness / "waku-agent", harness / "waku-agent-local7777"
    (main / ".git" / "worktrees" / "waku-agent-local7777").mkdir(parents=True)
    (main / ".env").write_text(f"ANTHROPIC_API_KEY={SECRET}\n")
    worktree.mkdir()
    (worktree / ".git").write_text(
        f"gitdir: {main / '.git' / 'worktrees' / 'waku-agent-local7777'}\n")
    (user_home / ".waku").mkdir()
    (user_home / ".waku" / ".env").write_text("TYPESAFE_API_KEY=not-a-model-key\n")
    return SimpleNamespace(user_home=user_home, main=main, worktree=worktree)


def _report(layout: SimpleNamespace, env: dict | None = None,
            startup: frozenset[str] = frozenset()) -> dict:
    return model_key_locations(cwd=layout.worktree, env=env or {},
                               user_home=layout.user_home, startup_names=startup)


def test_places_come_in_the_order_they_win(tmp_path):
    """Environment variables beat the folder's .env, which beats <home>/.env:
    load_dotenv never overrides a name that is already set."""
    report = _report(_seans_layout(tmp_path))
    wheres = [place["where"] for place in report["places"]]
    assert wheres == ["Environment variables",
                      "~/Developer/waku-agent-harness/waku-agent-local7777/.env",
                      "~/.waku/.env"]


def test_seans_layout_reads_as_it_was(tmp_path):
    layout = _seans_layout(tmp_path)
    report = _report(layout)
    states = [place["state"] for place in report["places"]]
    assert states == ["no model key.",
                      "not found, and no folder above it has a .env.",
                      "found, with no model key in it."]
    assert report["intro"].endswith("and found none:")
    # The main checkout's .env is named, and so is where to copy the line.
    assert len(report["unread"]) == 1
    hint = report["unread"][0]
    assert "git worktree" in hint
    assert "~/Developer/waku-agent-harness/waku-agent/.env" in hint
    assert "~/.waku/.env" in hint
    # Nothing was found, so the paste goes to the home .env.
    assert report["save_to"] == "~/.waku/.env"
    assert "from any folder" in report["save_note"]


def test_presence_is_reported_without_the_value(tmp_path):
    layout = _seans_layout(tmp_path)
    (layout.user_home / ".waku" / ".env").write_text(f"ANTHROPIC_API_KEY={SECRET}\n")
    report = _report(layout, env={"ANTHROPIC_API_KEY": SECRET})
    assert report["places"][2]["state"] == "found, with a model key in it."
    assert report["places"][2]["has_key"] is True
    assert SECRET not in json.dumps(report)
    assert SECRET not in "\n".join(cli_lines(report))


def test_the_environment_counts_only_names_set_before_any_env_loaded(tmp_path):
    """After the .env files load, os.environ holds their keys too. Only the
    names recorded before loading count as environment variables."""
    layout = _seans_layout(tmp_path)
    from_shell = _report(layout, env={"ANTHROPIC_API_KEY": "x"},
                         startup=frozenset({"ANTHROPIC_API_KEY"}))
    assert from_shell["places"][0] == {"where": "Environment variables", "path": False,
                                       "state": "a model key is set.", "has_key": True}
    from_file = _report(layout, env={"ANTHROPIC_API_KEY": "x"})
    assert from_file["places"][0]["has_key"] is False
    # The real record holds names only.
    assert all(isinstance(name, str) for name in config.STARTUP_ENV_NAMES)


def test_a_file_waku_does_not_read_is_only_said_to_exist(tmp_path):
    """The main checkout's .env holds a key, and the report still claims only
    that the file exists: Waku never opens a .env it does not load."""
    report = _report(_seans_layout(tmp_path))
    hint = report["unread"][0]
    assert "If your key is in that file" in hint
    assert "has a model key" not in hint and "with a model key" not in hint
    assert not any(place["has_key"] for place in report["places"])


def test_a_legacy_folder_env_is_named_when_home_is_elsewhere(tmp_path):
    layout = _seans_layout(tmp_path)
    (layout.worktree / ".waku").mkdir()
    (layout.worktree / ".waku" / ".env").write_text("OPENAI_API_KEY=x\n")
    report = _report(layout)
    assert any(".waku/.env exists" in line and "~/.waku" in line for line in report["unread"])


def test_a_key_edited_in_after_start_asks_for_a_restart(tmp_path):
    layout = _seans_layout(tmp_path)
    (layout.user_home / ".waku" / ".env").write_text("ANTHROPIC_API_KEY=x\n")
    assert "Restart Waku" in _report(layout, env={})["restart"]
    assert _report(layout, env={"ANTHROPIC_API_KEY": "x"})["restart"] == ""


def _provider_configured(cwd: Path, home: Path) -> bool:
    """Start a fresh Waku in `cwd` with `home` and no provider key in the
    environment, and ask whether any provider card reads as configured: the
    exact test the setup gate (setup.js needsSetup) applies."""
    env = {k: v for k, v in os.environ.items()
           if k not in {p.key_env for p in integrations.PROVIDERS.values()}}
    env.update({"WAKU_HOME": str(home), "PYTHONPATH": str(ROOT)})
    out = subprocess.run(
        [sys.executable, "-c",
         ("from waku.integrations import list_providers\n"
          "print(any(v.fields[0].configured for v in list_providers()))")],
        cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout.strip()
    return out == "True"


def test_the_gate_shows_only_when_no_place_holds_a_key(tmp_path):
    cwd, home = tmp_path / "anywhere", tmp_path / "home"
    cwd.mkdir()
    home.mkdir()
    assert _provider_configured(cwd, home) is False
    (home / ".env").write_text("ANTHROPIC_API_KEY=sk-home\n")
    assert _provider_configured(cwd, home) is True


@pytest.fixture
def save_env(monkeypatch, tmp_path):
    """A dashboard save in an empty folder, with a throwaway home and no real
    probe, rebuild or network call."""
    folder, home = tmp_path / "folder", tmp_path / "home"
    folder.mkdir()
    monkeypatch.chdir(folder)
    monkeypatch.setenv("WAKU_HOME", str(home))
    for name in ("ANTHROPIC_API_KEY", "WAKU_PROVIDER", "WAKU_MODEL", "WAKU_SMALL_MODEL"):
        monkeypatch.setenv(name, "")
    monkeypatch.setattr(integrations, "_provider_probe", lambda values: None)
    from waku.ops import browser_agent

    monkeypatch.setattr(browser_agent, "current", lambda: None)
    return SimpleNamespace(folder=folder, home=home)


def test_a_save_with_no_folder_env_goes_to_the_home_env(save_env):
    result = integrations.apply_provider("anthropic", key="sk-test-home", activate=False)
    assert result.ok, result.error
    target = save_env.home / ".env"
    assert "ANTHROPIC_API_KEY" in target.read_text()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not (save_env.folder / ".env").exists()


def test_a_save_with_a_folder_env_goes_to_that_file(save_env):
    """Spec 002: the folder's .env loads first and wins, so a save written to
    the home file could be hidden by it. The save goes where it will be read."""
    (save_env.folder / ".env").write_text("WAKU_PROVIDER=anthropic\n")
    result = integrations.apply_provider("anthropic", key="sk-test-folder", activate=False)
    assert result.ok, result.error
    assert "ANTHROPIC_API_KEY" in (save_env.folder / ".env").read_text()
    assert not (save_env.home / ".env").exists()


def test_every_writer_uses_the_one_rule():
    """Three places used to call find_dotenv themselves, each falling back to
    ./.env. One rule now decides where a save goes."""
    for rel in ("waku/integrations.py", "waku/ops/settings_api.py",
                "waku/ops/judgment_arena.py"):
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert 'or ".env"' not in source, rel
        assert "find_dotenv(usecwd=True)" not in source, rel


def test_the_setup_page_renders_the_places_and_the_target():
    source = SETUP.read_text(encoding="utf-8")
    assert "d.key_locations" in source
    for field in ("kl.intro", "kl.places", "kl.unread", "kl.restart", "kl.save_note"):
        assert field in source, field
    assert "The key is written to <code>.env</code> on this" not in source


def test_waku_connections_prints_the_model_key_lines(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WAKU_HOME", str(tmp_path / "home"))
    integrations.cli_main()
    out = capsys.readouterr().out
    assert "Model key" in out
    assert "Environment variables:" in out
    assert str(tmp_path / "home" / ".env") in out or "home/.env" in out
