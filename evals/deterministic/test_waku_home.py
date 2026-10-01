"""DETERMINISTIC EVAL — where Waku keeps its memory (spec 002, waku-agent#249).

Waku keeps its memory in ~/.waku, so the same assistant answers from every
folder. A person whose memory is still in a folder's ./.waku keeps that memory
until they copy it: Waku must never silently switch which memory answers.

On 2026-10-01 a maintainer's machine had a ~/.waku folder holding two
unrelated files and no state.db, while the real memory sat in a repo's
./.waku. A rule that checked for the ~/.waku folder would have switched that
person to an empty memory. These cases pin the rule that checks for state.db.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from waku import config
from waku.config import COPY_COMMAND, describe_home, home_notice, resolve_home


def _layout(tmp_path: Path, *, legacy_db: bool, global_dir: bool, global_db: bool):
    cwd, user_home = tmp_path / "project", tmp_path / "user"
    cwd.mkdir()
    user_home.mkdir()
    if legacy_db:
        (cwd / ".waku").mkdir()
        (cwd / ".waku" / "state.db").write_text("legacy")
    if global_dir or global_db:
        (user_home / ".waku").mkdir()
        (user_home / ".waku" / "message.txt").write_text("unrelated")
    if global_db:
        (user_home / ".waku" / "state.db").write_text("global")
    return cwd, user_home


def test_a_new_user_gets_waku_in_their_home_folder(tmp_path):
    cwd, user_home = _layout(tmp_path, legacy_db=False, global_dir=False, global_db=False)
    choice = resolve_home(env={}, cwd=cwd, user_home=user_home)
    assert choice == config.HomeChoice(user_home / ".waku", "global")
    assert home_notice(env={}, cwd=cwd, user_home=user_home) == ""


def test_waku_home_wins_over_everything(tmp_path):
    cwd, user_home = _layout(tmp_path, legacy_db=True, global_dir=True, global_db=True)
    choice = resolve_home(env={"WAKU_HOME": str(tmp_path / "x")}, cwd=cwd, user_home=user_home)
    assert choice == config.HomeChoice(tmp_path / "x", "WAKU_HOME")


def test_a_legacy_folder_keeps_answering_and_prints_the_copy_command(tmp_path):
    cwd, user_home = _layout(tmp_path, legacy_db=True, global_dir=False, global_db=False)
    assert resolve_home(env={}, cwd=cwd, user_home=user_home) == config.HomeChoice(cwd / ".waku", "legacy")
    notice = home_notice(env={}, cwd=cwd, user_home=user_home)
    assert "Waku is using ./.waku" in notice
    assert COPY_COMMAND in notice
    assert COPY_COMMAND == "mkdir -p ~/.waku && cp -R ./.waku/. ~/.waku/"


def test_a_home_folder_without_state_db_does_not_hide_legacy_memory(tmp_path):
    """The 2026-10-01 layout: ~/.waku exists, holds no state.db."""
    cwd, user_home = _layout(tmp_path, legacy_db=True, global_dir=True, global_db=False)
    assert resolve_home(env={}, cwd=cwd, user_home=user_home).rule == "legacy"


def test_the_printed_command_copies_into_home_without_nesting(tmp_path):
    cwd, user_home = _layout(tmp_path, legacy_db=True, global_dir=True, global_db=False)
    subprocess.run(["sh", "-c", COPY_COMMAND], cwd=cwd, check=True,
                   env={**os.environ, "HOME": str(user_home)})
    assert (user_home / ".waku" / "state.db").read_text() == "legacy"
    assert (user_home / ".waku" / "message.txt").exists()
    assert not (user_home / ".waku" / ".waku").exists()
    assert (cwd / ".waku" / "state.db").exists(), "the copy must leave the original in place"
    assert resolve_home(env={}, cwd=cwd, user_home=user_home).rule == "global"


def test_once_both_exist_home_wins_and_the_old_folder_is_named(tmp_path):
    cwd, user_home = _layout(tmp_path, legacy_db=True, global_dir=True, global_db=True)
    assert resolve_home(env={}, cwd=cwd, user_home=user_home) == config.HomeChoice(user_home / ".waku", "global")
    assert "ignoring ./.waku" in home_notice(env={}, cwd=cwd, user_home=user_home)


def test_running_from_the_home_folder_itself_prints_nothing(tmp_path):
    user_home = tmp_path / "user"
    (user_home / ".waku").mkdir(parents=True)
    (user_home / ".waku" / "state.db").write_text("global")
    assert resolve_home(env={}, cwd=user_home, user_home=user_home).rule == "global"
    assert home_notice(env={}, cwd=user_home, user_home=user_home) == ""


def test_settings_uses_the_resolved_home(tmp_path, monkeypatch):
    cwd, user_home = _layout(tmp_path, legacy_db=False, global_dir=False, global_db=False)
    monkeypatch.delenv("WAKU_HOME", raising=False)
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.chdir(cwd)
    assert config.Settings().home == user_home / ".waku"


def test_describe_home_names_the_rule(tmp_path):
    assert "set by WAKU_HOME" in describe_home(config.HomeChoice(tmp_path, "WAKU_HOME"))
    assert "older memory in this folder" in describe_home(config.HomeChoice(tmp_path, "legacy"))
    assert "default" in describe_home(config.HomeChoice(tmp_path, "global"))


def test_evals_never_resolve_to_a_real_home():
    """evals/conftest.py points every test at a throwaway home."""
    home = Path(os.environ["WAKU_HOME"]).resolve()
    assert home != (Path.home() / ".waku").resolve()
    assert home != (Path.cwd() / ".waku").resolve()


# --- Task 2: a global install finds its key in <home>/.env from any folder.
# config.py loads .env files at import, so each case runs in a fresh process.

def _import_config_in(cwd: Path, user_home: Path) -> dict:
    script = (
        "import json, os\n"
        "from waku import config\n"
        "print(json.dumps({'key': os.environ.get('ANTHROPIC_API_KEY', ''),\n"
        "                  'cwd_env': config.DOTENV_PATH,\n"
        "                  'home_env': config.HOME_DOTENV_PATH}))\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in ("WAKU_HOME", "ANTHROPIC_API_KEY")}
    env["HOME"] = str(user_home)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    out = subprocess.run([sys.executable, "-c", script], cwd=cwd, env=env,
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_key_in_the_home_env_is_found_from_an_unrelated_folder(tmp_path):
    cwd, user_home = _layout(tmp_path, legacy_db=False, global_dir=True, global_db=True)
    (user_home / ".waku" / ".env").write_text("ANTHROPIC_API_KEY=from-home\n")
    seen = _import_config_in(cwd, user_home)
    assert seen["key"] == "from-home"
    assert seen["home_env"] == str(user_home / ".waku" / ".env")
    assert seen["cwd_env"] == ""


def test_the_working_directory_env_wins_over_the_home_env(tmp_path):
    cwd, user_home = _layout(tmp_path, legacy_db=False, global_dir=True, global_db=True)
    (user_home / ".waku" / ".env").write_text("ANTHROPIC_API_KEY=from-home\n")
    (cwd / ".env").write_text("ANTHROPIC_API_KEY=from-cwd\n")
    assert _import_config_in(cwd, user_home)["key"] == "from-cwd"


def test_the_working_directory_env_can_choose_the_home(tmp_path):
    """WAKU_HOME set in the folder's .env decides which home's .env is read."""
    cwd, user_home = _layout(tmp_path, legacy_db=False, global_dir=False, global_db=False)
    other = tmp_path / "other-home"
    other.mkdir()
    (other / ".env").write_text("ANTHROPIC_API_KEY=from-other\n")
    (cwd / ".env").write_text(f"WAKU_HOME={other}\n")
    seen = _import_config_in(cwd, user_home)
    assert seen["key"] == "from-other"
    assert seen["home_env"] == str(other / ".env")


def test_the_no_key_message_points_at_the_home_env_when_no_env_exists(monkeypatch, tmp_path):
    from waku.loop import models
    monkeypatch.setattr(config, "DOTENV_PATH", "")
    monkeypatch.setattr(config, "HOME_DOTENV_PATH", "")
    monkeypatch.setenv("WAKU_HOME", str(tmp_path / "h"))
    msg = models._no_key_message("anthropic", "ANTHROPIC_API_KEY")
    assert "No .env found" in msg
    assert str(tmp_path / "h" / ".env") in msg
    assert "from any folder" in msg


def test_the_no_key_message_names_the_home_env_it_read(monkeypatch, tmp_path):
    from waku.loop import models
    monkeypatch.setattr(config, "DOTENV_PATH", "")
    monkeypatch.setattr(config, "HOME_DOTENV_PATH", str(tmp_path / "h" / ".env"))
    msg = models._no_key_message("anthropic", "ANTHROPIC_API_KEY")
    assert f"Add it to {tmp_path / 'h' / '.env'}" in msg


# --- Task 3: `waku mcp` reads the same home as the rest of Waku. It used to
# hard-code ./.waku and ignore WAKU_HOME, so its tokens could land in a second home.

def test_waku_mcp_lists_the_same_home_as_settings(tmp_path, monkeypatch):
    from waku.tools import mcp_cli
    monkeypatch.setenv("WAKU_HOME", str(tmp_path / "h"))
    seen = []
    monkeypatch.setattr(mcp_cli, "_list", lambda home: seen.append(home) or 0)
    assert mcp_cli.cli_main([]) == 0
    assert seen == [config.Settings().home] == [tmp_path / "h"]


def test_waku_mcp_has_no_home_of_its_own():
    from waku.tools import mcp_cli
    assert not hasattr(mcp_cli, "WAKU_HOME")
