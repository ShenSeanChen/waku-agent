"""DETERMINISTIC EVAL -- the Docker tier's probe survives a flaky `docker exec`.

evals/hosted_docker/dockerlib.py is the test-side Docker helper every Docker
test in groups C, E and F goes through. It needs a daemon to do its job, but
its CONTROL FLOW does not: `exec_in` returns a CompletedProcess, so the
retry logic can be driven here, offline, in milliseconds.

WHY THIS FILE EXISTS. `wait_for_listener` retries for thirty seconds, and
`probe_tcp` RAISES on any output that is not YES or NO -- which is right, since
a prober that is not alive would make every "A cannot reach B" assertion in
groups C and E pass for free. But the two together had a hole: one transient
`docker exec` failure on iteration 1 killed the loop and the remaining budget
was never spent. That is the exact moment `wait_for_listener` is called -- a
container that has just been created and is not yet running answers
`Error response from daemon: Container <id> is not running` -- so it is a CI
flake waiting to happen, in a job (C4) nobody watches closely yet.

The fix is not "catch everything": a probe that never runs must still fail, and
it must fail with the probe's own diagnosis rather than a generic timeout, or
the next reader spends an afternoon on the wrong end of it. The three tests
below pin all three shapes.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "evals" / "hosted_docker"))

import dockerlib  # noqa: E402

TRANSIENT = "Error response from daemon: Container 0a1b2c3d is not running"


def _proc(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["docker"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


@pytest.fixture()
def quiet(monkeypatch):
    """No `docker logs` shell-out and no real sleeping. Both are the loop's
    per-iteration cost, and neither is what these tests are about.

    The sleep is neutered by replacing dockerlib's OWN `time` binding, not by
    setting `sleep` on the stdlib time module. `monkeypatch.setattr(
    dockerlib.time, "sleep", ...)` reaches through to the one shared module
    object and every other library in the process -- pytest's own timeouts,
    a plugin's poller -- silently stops sleeping for the duration of the test.
    It was written that way first; this is module-local instead, and
    `monotonic` stays real so the deadline is a real deadline.
    """
    monkeypatch.setattr(dockerlib, "logs", lambda container: "the target's logs")
    monkeypatch.setattr(dockerlib, "time", SimpleNamespace(
        monotonic=time.monotonic, sleep=lambda seconds: None))
    return monkeypatch


def test_wait_for_listener_survives_one_transient_docker_exec_failure(quiet):
    """The container was created a millisecond ago and is not running YET.

    Without this, iteration 1 raises and the thirty-second budget is never
    spent -- so the fixture fails on the one condition it exists to wait out.
    """
    calls: list[int] = []

    def flaky_exec(container, cmd, *, user=None, check=True):
        calls.append(1)
        if len(calls) == 1:
            return _proc(1, "", TRANSIENT)
        return _proc(0, "YES\n")

    quiet.setattr(dockerlib, "exec_in", flaky_exec)

    dockerlib.wait_for_listener("0a1b2c3d", "127.0.0.1", 7777, timeout=5.0)

    assert len(calls) == 2, (
        "wait_for_listener did not retry after a transient `docker exec` "
        f"failure: it called exec_in {len(calls)} time(s). One failure on "
        "iteration 1 must not consume the whole timeout budget.")


def test_wait_for_listener_reraises_the_probes_own_failure_when_it_never_recovers(quiet):
    """A probe that never runs is still a failure, and the message has to be
    the probe's, not a generic timeout. `docker exec` failing for thirty
    seconds is a different bug from a listener that never bound, and a reader
    who is handed the wrong one of those loses an afternoon."""
    quiet.setattr(dockerlib, "exec_in",
                  lambda *args, **kwargs: _proc(1, "", TRANSIENT))

    with pytest.raises(dockerlib.DockerError) as caught:
        dockerlib.wait_for_listener("0a1b2c3d", "127.0.0.1", 7777, timeout=0.4)

    assert "the probe did not run" in str(caught.value), (
        "the deadline was reached with the probe never once running, and the "
        f"error raised was not the probe's own: {caught.value}")


def test_wait_for_listener_fails_loudly_when_the_target_simply_never_listens(quiet):
    """The probe ran fine every time and the answer was NO every time. That is
    a target that never came up, and it must NOT be read as a pass by whatever
    unreachability assertion follows."""
    quiet.setattr(dockerlib, "exec_in",
                  lambda *args, **kwargs: _proc(0, "NO ConnectionRefusedError\n"))

    with pytest.raises(AssertionError) as caught:
        dockerlib.wait_for_listener("0a1b2c3d", "127.0.0.1", 7777, timeout=0.4)

    message = str(caught.value)
    assert "the probe target never came up" in message, message
    assert "the target's logs" in message, (
        "the failure does not carry the target's logs, which is the only clue "
        f"the reader gets about why it never bound: {message}")


def test_probe_tcp_never_reports_a_dead_prober_as_a_blocked_connection(quiet):
    """The YES/NO protocol, pinned offline. A `docker exec` that failed for any
    reason must raise here, because `assert not probe_tcp(...)` reads a False
    as "correctly blocked" -- which is how an unreachability suite passes
    against a container that is not even running."""
    quiet.setattr(dockerlib, "exec_in",
                  lambda *args, **kwargs: _proc(1, "", "no such container"))

    with pytest.raises(dockerlib.DockerError) as caught:
        dockerlib.probe_tcp("0a1b2c3d", "127.0.0.1", 7777)

    assert "This is NOT 'the connection was blocked'" in str(caught.value)
