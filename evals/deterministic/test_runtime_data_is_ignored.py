"""DETERMINISTIC EVAL — runtime data and secrets never become commits.

waku runs on people's own machines with their own keys, and it writes a lot of
personal state: facts about the user, their calendar, a spend ledger, message
outboxes, traces of every turn. None of it belongs in git. This pins the
.gitignore rules that keep it out, because that file is edited by hand and a
missing line is invisible until the day it isn't.

The gap this was written for (2026-07-26): .gitignore listed `.waku/` by exact
name. When the Discord bot was given its OWN memory via DISCORD_HOME — the fix
for a bot answering strangers out of the maintainer's personal store — that
second home was called `.waku-discord/`, matched nothing, and its SOUL.md,
usage.jsonl and outbox/ were tracked. A privacy fix had quietly created a
different privacy hole. `.waku-*/` closes it for any alternate home.

These call `git check-ignore`, so they test what git ACTUALLY does with the
committed .gitignore — not what a regex here thinks it should do.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def ignored(rel: str) -> bool:
    """True when git would refuse to track this path. -q: exit 0 = ignored."""
    return subprocess.run(
        ["git", "check-ignore", "-q", rel], cwd=REPO, capture_output=True, check=False
    ).returncode == 0


# Every runtime artifact a home can hold, for the DEFAULT home and for an
# alternate one (DISCORD_HOME, or any WAKU_HOME a user points elsewhere).
ARTIFACTS = ["state.db", "SOUL.md", "usage.jsonl", "calendar.ics",
             "traces/2026-07-26.jsonl", "outbox/msg-1.txt"]


@pytest.mark.parametrize("home", [".waku", ".waku-discord", ".waku-demo"])
@pytest.mark.parametrize("artifact", ARTIFACTS)
def test_no_agent_home_leaks_its_runtime_data(home, artifact):
    """A second agent home is a normal thing to have — one per gateway, one for
    a demo recording. Every one of them must be invisible to git."""
    assert ignored(f"{home}/{artifact}"), (
        f"{home}/{artifact} would be COMMITTED. That is the user's private data."
    )


@pytest.mark.parametrize("secret", [
    ".env",
    "credentials.json",            # Google OAuth client secret
    "client_secret_123.json",      # what the Google console actually names it
    "token.json",                  # a refresh token, worse than the key
    ".gcp/service-account.json",
])
def test_secrets_are_ignored(secret):
    """The README tells people to drop credentials.json in the repo root, so the
    ignore rule is load-bearing rather than belt-and-braces. Sean caught this
    one missing on 2026-07-26."""
    assert ignored(secret), f"{secret} is not ignored — a key could be pushed"


def test_nothing_from_a_runtime_home_is_currently_tracked():
    """The rules above only help going forward. If a file was committed BEFORE
    the rule existed, .gitignore does not untrack it — git keeps following it
    forever and check-ignore still says 'ignored'. This is the check that
    catches that: ask git what it is actually tracking."""
    # .env.example is the documented TEMPLATE and is tracked on purpose — every
    # value in it is empty, commented, or a placeholder path. It is the one
    # ".env*" file that belongs in the repo.
    allowed = {".env.example"}
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    leaked = [f for f in tracked
              if f not in allowed
              and (f.startswith((".waku", ".env"))
                   or f.endswith(("credentials.json", "token.json")))]
    assert leaked == [], f"private files are in the index: {leaked}"


def test_the_env_template_carries_no_real_values():
    """.env.example is tracked, so a pasted key there is a pushed key. Every
    assignment must be empty or a placeholder — the file teaches which variables
    exist, never what they are set to."""
    lines = (REPO / ".env.example").read_text(encoding="utf-8").splitlines()
    filled = [ln for ln in lines
              if "=" in ln and not ln.lstrip().startswith("#")
              and ln.split("=", 1)[1].strip().strip("\"'")]
    # WAKU_PROVIDER is a non-secret default and is meant to have a value.
    unexpected = [ln for ln in filled if not ln.startswith("WAKU_PROVIDER=")]
    assert unexpected == [], f"values committed in .env.example: {unexpected}"
