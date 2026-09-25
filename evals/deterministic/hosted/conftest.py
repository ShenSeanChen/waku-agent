"""Skip this directory when hosted/ is not in the checkout.

The sdist ships evals/ and excludes "/hosted" (pyproject.toml), so a test run
from an unpacked sdist finds these files with nothing behind them. That must
skip, not fail: a contributor who runs the suite from a download should see
"skipped", not an import error from a directory they were never given.

Every test B2, B3 and B4 add here inherits this. None of them has to repeat
it, and none of them may import `hosted` at module scope without it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HOSTED = Path(__file__).resolve().parents[3] / "hosted"

if not HOSTED.is_dir():
    collect_ignore_glob = ["*.py"]
else:
    # gatewaylib.py is a sibling module, not a package.
    # evals/hosted_docker/conftest.py does the same for dockerlib.py.
    sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture
def harness(tmp_path):
    """A gateway with the recording forwarder. Each test drives it inside one
    asyncio.run and calls `await harness.stop()` before returning.

    The fixture lives here rather than in gatewaylib.py because a fixture is
    only collected from a test module or a conftest: imported into a test
    module it is also an unused name to ruff, and `pytest_plugins` in a
    non-root conftest is an error on pytest 9. gatewaylib.py still owns the
    two classes; this is the one line that makes them a fixture.
    """
    from gatewaylib import FakeSpawner, Harness

    return Harness(tmp_path, FakeSpawner())


def _wired(tmp_path, max_running):
    """A gateway with the real forwarder in front of a fake container, on a VM
    with room for `max_running` of them.

    Here and not in gatewaylib.py for the reason `harness` gives above: a
    fixture is only collected from a test module or a conftest. test_gateway.py
    and test_admin.py both take these, so this is what lets them share them
    without importing a fixture out of a test module.

    The cap is given to the Harness rather than written into
    `fleet._max_running` by a test. A test that reaches into a private
    attribute stops being true when the attribute is renamed, and the state it
    creates is not the state a real small VM is in -- the Gateway's own config
    would still say four.
    """
    from gatewaylib import FakeContainer, FakeSpawner, Harness

    container = FakeContainer()
    container.start()
    harness = Harness(tmp_path, FakeSpawner(), max_running=max_running)
    harness.use_real_forwarding(container)
    yield harness
    container.stop()


@pytest.fixture
def wired(tmp_path):
    yield from _wired(tmp_path, 4)


@pytest.fixture
def wired_one_slot(tmp_path):
    """Room for exactly one container: the cap binds on the second tenant."""
    yield from _wired(tmp_path, 1)


@pytest.fixture
def wired_two_slots(tmp_path):
    """Room for two, which is the smallest VM on which "least recently used"
    is a choice rather than the only candidate."""
    yield from _wired(tmp_path, 2)


@pytest.fixture
def sock_dir():
    """A short directory for Unix sockets.

    AF_UNIX caps a path at 104 bytes on macOS and pytest spells the test's
    name into tmp_path, so a socket under tmp_path raises "AF_UNIX path too
    long" for tests with long names. test_admin.py and test_gateway.py both
    bind sockets, so it lives here rather than in either of them.
    """
    import tempfile

    with tempfile.TemporaryDirectory(prefix="waku") as short:
        yield Path(short)
