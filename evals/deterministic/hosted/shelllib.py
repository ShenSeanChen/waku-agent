"""Drive the deploy scripts with a real bash and a stubbed outside world.

THE IDIOM IS GROUP C'S. test_spawner_restore.py runs the real code with the
outside world faked, because the thing under test is the ORDER of the commands
and the refusals between them, not what the commands do. The same is true of
every script in hosted/deploy/: what can go wrong is a restore that runs before
a stop, a snapshot taken from a staging slot with no manifest, or an installer
that carries on after a refusal.

WHAT A STUB RECORDS. Each stub appends its own name and arguments to
$WAKU_CALLS, one line per call, then exits 0 -- so a test reads the call log as
a list and can assert ORDER, not just presence. A stub with a body in `bodies`
replaces that behaviour entirely, which is how `docker` comes to print JSON or
how `curl` comes to fail.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[3] / "hosted" / "deploy"

_RECORDER = """#!/bin/sh
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$WAKU_CALLS"
exit 0
"""


def stub_path(tmp_path: Path, names: Iterable[str],
              bodies: Mapping[str, str] | None = None) -> Path:
    """A directory of executables to put FIRST on PATH."""
    bodies = dict(bodies or {})
    directory = tmp_path / "stubs"
    directory.mkdir(exist_ok=True)
    for name in names:
        stub = directory / name
        stub.write_text(bodies.get(name, _RECORDER), encoding="utf-8")
        stub.chmod(0o755)
    return directory


def run(script: Path, args: Sequence[str], *, tmp_path: Path,
        env: Mapping[str, str] | None = None,
        stubs: Iterable[str] = (),
        bodies: Mapping[str, str] | None = None
        ) -> subprocess.CompletedProcess[str]:
    """Run `script` with the stubs first on PATH.

    Never `check=True`: every test here is about what the script decided, and
    an exit code is the decision.
    """
    calls_file = tmp_path / "calls.log"
    calls_file.touch()
    directory = stub_path(tmp_path, stubs, bodies)
    # The real PATH stays on the end: bash itself, awk, jq and sed are not the
    # things under test, and a test that stubbed them would be testing nothing.
    environment = {
        **os.environ,
        "PATH": f"{directory}{os.pathsep}{os.environ['PATH']}",
        "WAKU_CALLS": str(calls_file),
    }
    environment.update(env or {})
    return subprocess.run(["bash", str(script), *args],
                          capture_output=True, text=True, env=environment,
                          timeout=120, check=False)


def calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line]


def call_function(source: Path, call: str, *,
                  env: Mapping[str, str] | None = None
                  ) -> subprocess.CompletedProcess[str]:
    """Source a file of functions and call one of them.

    `call` is a shell command line, already quoted by the caller. checks.sh is
    written to be sourced, so this is exactly how install.sh reaches it -- and
    `set -euo pipefail` here is the caller's shell state, which is what makes
    waku_resolvers' pipeline fail on an empty result rather than succeed
    silently.
    """
    environment = {**os.environ, **(env or {})}
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail; . "{source}"; {call}'],
        capture_output=True, text=True, env=environment, timeout=60, check=False)
