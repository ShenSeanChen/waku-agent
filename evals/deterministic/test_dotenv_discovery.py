"""DETERMINISTIC EVAL — finding the user's .env, and saying so when we cannot.

Reported 2026-07-31 from a clean install: standing in the waku-agent folder,
with a .env holding a valid ANTHROPIC_API_KEY, `waku brief` answered

    No API key for provider 'anthropic'. Set ANTHROPIC_API_KEY in .env
    (see .env.example).

Both halves of that sentence were wrong. The .env was right there, and
.env.example does not exist unless you cloned the repo.

The cause: bare `load_dotenv()` searches upward from the FILE THAT CALLED IT,
not from the working directory. In a git checkout config.py sits inside the
project, so walking up from it lands on the project's .env by luck. Installed
from PyPI it walks up from site-packages, hits /, and finds nothing.

That is why no test caught it: every test runs from the checkout, where the
broken behaviour and the correct behaviour are indistinguishable. These tests
therefore assert on the RULE (usecwd) rather than on the outcome in this repo,
because the outcome in this repo is green either way.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
import textwrap

from waku import config
from waku.loop import models


def test_dotenv_is_found_from_the_working_directory():
    """THE regression. `find_dotenv()` without usecwd resolves relative to
    waku's own source; the user is standing somewhere else entirely."""
    src = inspect.getsource(config._load_env)
    assert "usecwd=True" in src, (
        "load_dotenv is searching from waku's install location again — an "
        "installed Waku will ignore the user's .env and claim it has no key"
    )


def test_the_loaded_path_is_recorded():
    """A user with three .env files needs to know which one won. Recording the
    path is also what lets the error message say where to add the line."""
    assert hasattr(config, "DOTENV_PATH")
    assert isinstance(config.DOTENV_PATH, str)


def test_a_subdirectory_still_finds_the_project_env(tmp_path):
    """The upward walk is kept deliberately: running `waku` from a subfolder of
    your project should find the .env at its root, the rule git and pytest
    already taught everyone. Only the STARTING POINT changes."""
    root = tmp_path / "proj"
    (root / "deep" / "nested").mkdir(parents=True)
    (root / ".env").write_text("ANTHROPIC_API_KEY=from-project-root\n", encoding="utf-8")
    # A clean environment on purpose. load_dotenv does NOT override a variable
    # that is already exported — correct behaviour (a real exported key should
    # beat a file) but it means this suite, which runs with ANTHROPIC_API_KEY
    # blanked, would otherwise measure the blank instead of the .env.
    import os

    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    out = subprocess.run(
        [sys.executable, "-c", textwrap.dedent("""
            from dotenv import find_dotenv, load_dotenv
            import os
            load_dotenv(find_dotenv(usecwd=True))
            print(os.getenv("ANTHROPIC_API_KEY", ""))
        """)],
        cwd=root / "deep" / "nested", env=env,
        capture_output=True, text=True, check=False)
    assert out.stdout.strip() == "from-project-root", out


def test_the_error_never_points_at_a_file_pip_users_do_not_have():
    """.env.example ships only in the git checkout. Telling someone who ran
    `pip install waku-agent` to 'see .env.example' is a dead end, and it was
    the ONLY instruction the old message gave."""
    msg = models._no_key_message("anthropic", "ANTHROPIC_API_KEY")
    assert ".env.example" not in msg


def test_the_error_says_how_to_get_a_key():
    msg = models._no_key_message("anthropic", "ANTHROPIC_API_KEY")
    assert "https://" in msg, "no URL — the reader has to go and search"
    assert "ANTHROPIC_API_KEY" in msg


def test_the_error_names_the_env_file_in_play():
    """'Set it in .env' is ambiguous the moment more than one .env exists. Say
    which file was loaded, or say plainly that none was found and where to
    create one."""
    msg = models._no_key_message("anthropic", "ANTHROPIC_API_KEY")
    assert (config.DOTENV_PATH and config.DOTENV_PATH in msg) or "No .env found" in msg


def test_the_error_mentions_the_other_providers():
    """Waku speaks to eleven providers. Naming one made it look like a
    single-vendor tool to anyone who hit this on their first run — which is
    everyone who hits it."""
    msg = models._no_key_message("anthropic", "ANTHROPIC_API_KEY")
    for other in ("openai", "gemini", "openrouter"):
        assert other in msg
    assert "WAKU_PROVIDER" in msg


def test_every_provider_has_somewhere_to_get_a_key():
    """A provider in the table with no URL is a dead end for whoever picks it."""
    missing = sorted(set(models.PROVIDERS) - set(models.KEY_URLS))
    assert missing == [], f"no key URL for: {missing}"
