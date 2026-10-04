"""Where Waku looked for a model key, in order, and what it found (spec 013).

The setup page and `waku connections` both say this, so a person who has a key
somewhere can see why Waku did not find it. Before this, the page said "paste a
key" to someone whose key sat in a folder Waku does not read from a git
worktree, and named no place at all.

WHAT THIS READS. Only the two .env files Waku already loads at startup
(waku/config.py): the working directory's, found by walking upward, and
<home>/.env. Each one is parsed for the NAMES that hold a value; the values are
dropped inside `_names_with_values` and never reach a return value, a log line
or the dashboard. Every other file named here is checked for existence only,
and the report never claims it holds a key (public hard rule 2).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from waku import config


def _model_key_names() -> frozenset[str]:
    """The variables that count as a model key: every visible provider's
    key_env, the same rows the setup gate's provider cards are built from."""
    from waku.loop.models import PROVIDERS

    return frozenset(p.key_env for p in PROVIDERS.values() if p.is_visible())


def _names_with_values(path: Path) -> frozenset[str]:
    """The names in a .env file that hold a non-empty value. Values are read by
    python-dotenv and discarded here; only the names leave this function."""
    from dotenv import dotenv_values

    try:
        return frozenset(name for name, value in dotenv_values(path).items() if value)
    except (OSError, UnicodeDecodeError):
        return frozenset()


def tilde(path: Path | str, user_home: Path | None = None) -> str:
    """A path as a person reads it: their home folder shown as ~."""
    user_home = Path.home() if user_home is None else user_home
    text, home = str(path), str(user_home)
    if text == home:
        return "~"
    return "~" + text[len(home):] if text.startswith(home + os.sep) else text


def _worktree_main(cwd: Path) -> Path | None:
    """The main checkout of the git worktree `cwd` sits in, or None.

    A worktree's `.git` is a file whose one line reads
    `gitdir: <main>/.git/worktrees/<name>`. This walks up to the first `.git`,
    as git does, and reads it only when it is a file.
    """
    for folder in (cwd, *cwd.parents):
        marker = folder / ".git"
        if marker.is_dir():
            return None
        if marker.is_file():
            try:
                line = marker.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                return None
            if not line.startswith("gitdir:"):
                return None
            gitdir = Path(line.removeprefix("gitdir:").strip())
            if not gitdir.is_absolute():
                gitdir = (folder / gitdir).resolve()
            if gitdir.parent.name != "worktrees" or gitdir.parent.parent.name != ".git":
                return None
            return gitdir.parent.parent.parent
    return None


def model_key_locations(cwd: Path | None = None, env: Mapping[str, str] | None = None,
                        user_home: Path | None = None,
                        startup_names: frozenset[str] | None = None) -> dict:
    """The places Waku reads a model key from, in the order they win, plus the
    .env files it knows about and does not read, and where a pasted key goes.

    Every argument defaults to the running process; the evals pass their own.
    The result holds paths and yes-or-no answers only.
    """
    cwd = Path.cwd() if cwd is None else cwd
    env = os.environ if env is None else env
    user_home = Path.home() if user_home is None else user_home
    startup_names = config.STARTUP_ENV_NAMES if startup_names is None else startup_names
    keys = _model_key_names()
    home = config.resolve_home(env=env, cwd=cwd, user_home=user_home)
    home_env = home.path / ".env"

    def show(path: Path | str) -> str:
        return tilde(path, user_home)

    def file_state(path: Path) -> tuple[str, bool]:
        if not path.is_file():
            return "not found.", False
        if _names_with_values(path) & keys:
            return "found, with a model key in it.", True
        return "found, with no model key in it.", False

    env_has = bool(startup_names & keys)
    places = [{"where": "Environment variables", "path": False,
               "state": "a model key is set." if env_has else "no model key.",
               "has_key": env_has}]
    found = config.find_env_upward(cwd)
    if found:
        state, has = file_state(Path(found))
        places.append({"where": show(found), "path": True, "state": state, "has_key": has})
    else:
        places.append({"where": show(cwd / ".env"), "path": True,
                       "state": "not found, and no folder above it has a .env.",
                       "has_key": False})
    if found and Path(found).resolve() == home_env.resolve():
        places.append({"where": show(home_env), "path": True,
                       "state": "the same file as above.",
                       "has_key": False})
    else:
        state, has = file_state(home_env)
        places.append({"where": show(home_env), "path": True, "state": state,
                       "has_key": has})

    # .env files Waku knows about and does not read. Existence only.
    unread: list[str] = []
    read_files = {Path(p).resolve() for p in (found, home_env) if p}
    main = _worktree_main(cwd)
    if main is not None and (main / ".env").is_file() and (main / ".env").resolve() not in read_files:
        unread.append(
            f"This folder is a git worktree. Its main checkout has {show(main / '.env')}, "
            f"and Waku does not read that file from here. If your key is in that file, "
            f"start Waku in {show(main)}, or copy the key's line into {show(home_env)}.")
    legacy_env = cwd / ".waku" / ".env"
    if legacy_env.is_file() and legacy_env.resolve() not in read_files:
        unread.append(
            f"{show(legacy_env)} exists, and Waku does not read it, because Waku now keeps "
            f"its files in {show(home.path)}. If your key is in that file, copy the key's "
            f"line into {show(home_env)}.")
    global_env = user_home / ".waku" / ".env"
    if (home.rule != "global" and global_env.is_file()
            and global_env.resolve() not in read_files | {legacy_env.resolve()}):
        why = ("WAKU_HOME points Waku at" if home.rule == "WAKU_HOME"
               else "the older memory in this folder makes Waku use")
        unread.append(
            f"{show(global_env)} exists, and Waku does not read it, because {why} "
            f"{show(home.path)}. If your key is in that file, copy the key's line "
            f"into {show(home_env)}.")

    any_key = any(place["has_key"] for place in places)
    # A file holds a key the running process never loaded: someone edited it
    # after Waku started. Only a restart reads it.
    restart = (any(place["has_key"] for place in places[1:])
               and not any(env.get(name) for name in keys))
    target, is_home = config.env_write_target(cwd=cwd, env=env, user_home=user_home)
    return {
        "intro": ("Waku looked for a model key in these places, in this order:" if any_key
                  else "Waku looked for a model key in these places, in this order, "
                       "and found none:"),
        "places": places,
        "unread": unread,
        "restart": ("A file above holds a model key that this running Waku has not read. "
                    "Restart Waku to read it.") if restart else "",
        "save_to": show(target),
        "save_note": (f"A key you paste here is written to {show(target)}, so Waku finds it "
                      f"from any folder." if is_home else
                      f"A key you paste here is written to {show(target)}. Waku reads that "
                      f"file only when you start it in {show(target.parent)} or a folder "
                      f"inside it."),
    }


def cli_lines(report: dict) -> list[str]:
    """The same report as plain lines for `waku connections`."""
    lines = [f"{place['where']}: {place['state']}" for place in report["places"]]
    lines += report["unread"]
    if report["restart"]:
        lines.append(report["restart"])
    lines.append(f"A key saved from the dashboard is written to {report['save_to']}.")
    return lines
