"""DETERMINISTIC EVAL -- the Engine's log stream, and what `list` will report.

Two things the spawner hands onward that nothing else checks: the text an
operator reads when a container fails, and the address the gateway forwards to.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from hosted.core import tenant
from hosted.ports.runtime import RunningContainer
from hosted.spawner import docker as docker_mod
from hosted.spawner import template
from hosted.spawner.engine import demultiplex


def _frame(stream: int, text: bytes) -> bytes:
    """One Docker log frame: stream byte, three zero bytes, BE uint32 length."""
    return struct.pack(">BxxxI", stream, len(text)) + text


def test_a_framed_log_stream_comes_back_as_the_text_it_carries():
    """C2-9. `GET /containers/{id}/logs` on a non-TTY container is FRAMED, and
    _run_to_completion puts this string into the RuntimeError an operator reads
    when a backup or a provision fails on the VM. Returned verbatim, the
    headers decode as control characters rather than raising -- unreadable
    rather than broken, which is worse, because nothing said so."""
    payload = _frame(1, b"provisioning\n") + _frame(2, b"Traceback\n") + _frame(1, b"done\n")
    assert demultiplex(payload) == "provisioning\nTraceback\ndone\n"
    assert "\x00" not in demultiplex(payload), (
        "a frame header survived into the operator-facing text")


def test_an_unframed_stream_is_returned_unchanged():
    """A container started WITH a TTY is not framed, and this cannot ask which
    it got. Unframed input must come back byte for byte rather than being
    chopped by a header that was never there."""
    assert demultiplex(b"plain output\nwith two lines\n") == \
        "plain output\nwith two lines\n"
    assert demultiplex(b"") == ""


def test_a_truncated_frame_keeps_its_bytes_rather_than_dropping_them():
    """`tail=200` can cut mid-frame. The remainder is returned as-is: a log
    that ends in the middle of a line is still the log, and silently dropping
    the tail is how the one line that explains a failure goes missing."""
    payload = _frame(1, b"first\n") + struct.pack(">BxxxI", 1, 999) + b"cut off here"
    out = demultiplex(payload)
    assert out.startswith("first\n")
    assert "cut off here" in out


def test_a_body_that_only_looks_framed_is_not_eaten():
    """The header test has to be strict, because ordinary output can begin with
    a low byte. Three zero bytes at offsets 1..3 is what distinguishes a frame,
    and a body without them is not one."""
    assert demultiplex(b"\x01abc\x00\x00\x00\x05hello") == "\x01abc\x00\x00\x00\x05hello"


class _ListEngine:
    def __init__(self, entries):
        self._entries = entries

    async def containers(self, *, label=None, all_states=False):
        return self._entries


def _entry(tenant_id: str, address: str) -> dict:
    return {
        "Id": "deadbeefcafe",
        "Labels": {template.LABEL_TENANT: tenant_id,
                   template.LABEL_KIND: template.KIND_TENANT},
        "NetworkSettings": {"Networks": {
            tenant.TENANT_NETWORK: {"IPAddress": address}}},
    }


CONFIG = template.SpawnerConfig(
    tenant_root=docker_mod.Path("/srv/waku/tenants"),
    archive_root=docker_mod.Path("/srv/waku/archive"),
    staging_root=docker_mod.Path("/srv/waku/staging"),
    tenant_image="waku-tenant:test",
    services_image="waku-services:test",
    platform_base_url="http://10.88.0.1:8788",
    platform_model="a-model",
    platform_small_model="a-model",
    tenant_disk_bytes=1073741824,
    data_device="none",
    seccomp_profile="{}",
)
GOOD = "k3fq7x2mza4b"


def _listed(entries) -> list[RunningContainer]:
    runtime = docker_mod.DockerRuntime(CONFIG, _ListEngine(entries))
    return asyncio.run(runtime.list())


def test_list_reports_a_tenant_on_a_real_tenant_address():
    """The presence half. Without it every absence below passes against a
    `list` that returns nothing at all."""
    address = tenant.address_for_project(tenant.FIRST_PROJECT_ID)
    assert _listed([_entry(GOOD, address)]) == [
        RunningContainer(tenant_id=GOOD, address=address,
                         port=template.DASHBOARD_PORT)]


@pytest.mark.parametrize("address", [
    "10.89.0.5",            # the inspect bridge, not a tenant address
    "172.17.0.2",           # Docker's own default pool
    "10.88.255.7",          # inside DYNAMIC_RANGE, which no project id derives
    "10.88.0.1",            # the bridge gateway, where the proxy listens
    "127.0.0.1",
    "not-an-address",
    "",
])
def test_list_refuses_an_address_no_project_id_derives(address):
    """C2-10. The gateway FORWARDS to whatever `list` returns, so an address
    off the tenant subnet is a request the platform makes somewhere it never
    meant to. The id beside it is already validated; this is the same rule for
    the other half of the pair."""
    assert _listed([_entry(GOOD, address)]) == []


def test_list_still_refuses_a_label_that_is_not_a_tenant_id():
    """Unchanged by C2-10, and asserted here so the new address check cannot be
    written in a way that replaces the id check rather than joining it."""
    good_address = tenant.address_for_project(tenant.FIRST_PROJECT_ID)
    assert _listed([_entry("NOT-AN-ID", good_address)]) == []
    assert _listed([_entry("../../etc", good_address)]) == []


def test_a_backup_refuses_to_chown_through_a_symlinked_staging_directory(tmp_path):
    """C2-11. Path.mkdir(exist_ok=True) re-checks with is_dir(), which FOLLOWS
    a link, and os.chown follows too -- so an existing symlink at
    <staging_root>/<id> would hand UID 10001 ownership of whatever it points
    at, as root. Not reachable today, because no tenant mount includes
    staging_root; refused anyway, because this process is root and the subject
    of the module is planted links."""
    config = template.SpawnerConfig(**{**CONFIG.__dict__,
                                       "staging_root": tmp_path / "staging"})
    victim = tmp_path / "victim"
    victim.mkdir()
    (tmp_path / "staging").mkdir()
    (tmp_path / "staging" / GOOD).symlink_to(victim)
    runtime = docker_mod.DockerRuntime(config, _ListEngine([]))
    with pytest.raises(RuntimeError, match="symlink"):
        asyncio.run(runtime.task(GOOD, "backup"))


# --- which Docker statuses each verb treats as "the intent holds" ----------


class _Answer:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self._body = body

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class _FakeSession:
    """Just enough aiohttp for Engine._call: a request() returning an async
    context manager with .status and .read()."""

    def __init__(self, status: int, body: bytes = b"") -> None:
        self._answer = _Answer(status, body)
        self.seen: list[tuple] = []

    def request(self, method, url, json=None, params=None):
        self.seen.append((method, url, params))
        return self._answer


def _engine_with(status: int, body: bytes = b""):
    from hosted.spawner.engine import Engine
    engine = Engine()
    session = _FakeSession(status, body)
    engine._session = session
    return engine, session


@pytest.mark.parametrize("status", [204, 404, 409])
def test_remove_accepts_every_status_that_means_the_container_is_going(status):
    """GC-8. 204 removed, 404 already gone, 409 REMOVAL ALREADY IN PROGRESS.

    409 is the AutoRemove reaper: a tenant container that has just exited is
    being removed by the daemon, and a DELETE arriving in that window answers
    "removal of container ... is already in progress". `stop()` calls remove on
    the START HOT PATH precisely to clear a name the reaper may be mid-way
    through, so treating 409 as an error turned the race it exists to absorb
    into an EngineError on every affected start.

    The comment above that call claimed the removal closed the 409 race while
    `expect` did not include 409, which is the third over-claiming docstring
    this task has had to correct.
    """
    engine, session = _engine_with(status)
    asyncio.run(engine.remove("waku-tenant-k3fq7x2mza4b"))
    assert session.seen[0][0] == "DELETE"


@pytest.mark.parametrize("status", [500, 502, 400])
def test_remove_still_raises_on_a_status_that_means_something_went_wrong(status):
    """The other direction: widening `expect` must not turn into accepting
    everything. A 500 from the daemon is a real failure and the spawner has to
    hear it."""
    from hosted.spawner.engine import EngineError

    engine, _session = _engine_with(status, b"boom")
    with pytest.raises(EngineError):
        asyncio.run(engine.remove("waku-tenant-k3fq7x2mza4b"))


@pytest.mark.parametrize("status", [204, 304, 404])
def test_stop_accepts_every_status_that_means_the_container_is_not_running(status):
    """304 already stopped, 404 already gone (AutoRemove). `stop` has to be
    idempotent because the gateway calls it before every start."""
    engine, _session = _engine_with(status)
    asyncio.run(engine.stop("waku-tenant-k3fq7x2mza4b"))


@pytest.mark.parametrize("status", [204, 304])
def test_start_accepts_already_started(status):
    """304 is what a retry after a timeout reaches, and it is not an error:
    the container the caller wanted is running."""
    engine, _session = _engine_with(status)
    asyncio.run(engine.start("waku-tenant-k3fq7x2mza4b"))
