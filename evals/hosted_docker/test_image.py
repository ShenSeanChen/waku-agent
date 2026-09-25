"""DETERMINISTIC EVAL, THIRD TIER -- the two images, built and run.

Task C1 of spec 001. What this file proves, and what it does not:

  IT PROVES, by reading the BUILT IMAGE and the RUNNING CONTAINER:
    - each image's /app holds what its Dockerfile COPYs and nothing more --
      asserted in BOTH directions, because a listing test with only negations
      passes on an empty listing and with the COPY lines deleted
    - that the per-Dockerfile allowlist is READ AT ALL, by planting a
      secret-shaped file inside an admitted tree and watching it stay out
      while its non-secret sibling arrives
    - under a read-only root, with only /data, /work and a 256 MB tmpfs /tmp
      writable, a full turn, a settings save and a SQL console query all
      succeed (design section 10.6)
    - the container's writable layer is untouched after those three
    - the .env the tenant reads is the one in /work, by saving through the
      dashboard and reading the host side of the mount back

  IT DOES NOT PROVE anything about the Dockerfile's or the ignore file's TEXT.
  That is evals/deterministic/hosted/test_image_context.py, which is a DRIFT
  CHECK on the allowlist's shape and says so. Asserting an exclude string
  instead of the built artefact is the defect this spec has now found five
  times; the assertions below read `docker run ... ls -a`.

EVERY TEST HERE IS UNEXECUTED as of task C1: this machine has Docker 28.0.4
installed and no daemon running. C4 brings the job that runs them, and its
first green run is this file's first evidence.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import dockerlib
import pytest

from hosted.core.provision import render_env
from hosted.spawner import template


def _config_for_tests(tenant_image: str, *, upstream_url: str) -> template.SpawnerConfig:
    """A SpawnerConfig for this file's own container, so the nine environment
    variables come from hosted/spawner/template.py and not from a second copy.

    Only Env is taken from the body it builds: this test starts its container
    with dockerlib, on its own network, with C1's two flags, because what it is
    testing is the IMAGE. The isolation fields are C2's and are tested in
    evals/hosted_docker/test_spawner.py against the running kernel.
    """
    return template.SpawnerConfig(
        tenant_root=Path("/unused-by-this-test"),
        archive_root=Path("/unused-by-this-test"),
        staging_root=Path("/unused-by-this-test"),
        tenant_image=tenant_image,
        services_image="waku-services:test",
        platform_base_url=upstream_url,
        platform_model="waku-test-model",
        platform_small_model="waku-test-model",
        tenant_disk_bytes=64 * 1024 * 1024,
        data_device="none",
        seccomp_profile=(dockerlib.REPO / "hosted" / "image"
                         / "seccomp.json").read_text(encoding="utf-8"),
    )

# The exact set of /app entries each image's Dockerfile puts there, plus what
# the build creates. `leaked` catches anything extra; `MUST_BE_IN_*` catches
# the empty listing and the deleted COPY, which a negation-only test reads as
# a pass.
ADMITTED_IN_APP = {
    ".", "..",
    "pyproject.toml", "uv.lock", "README.md", "LICENSE", "LICENSE-BRAND",
    "waku", "skills",
    ".venv",          # created by `uv sync`
    "uv.lock.lock",   # uv's own lock file, if the version in use writes one.
                      # Speculative: delete it once C4 has run and shown the
                      # real listing. It fails in the safe direction (slack in
                      # the leak set, never a missing presence assertion).
}
MUST_BE_IN_APP = {
    "pyproject.toml", "uv.lock", "README.md", "LICENSE", "LICENSE-BRAND",
    "waku", "skills", ".venv",
}

ADMITTED_IN_SERVICES_APP = {
    ".", "..",
    "pyproject.toml", "uv.lock", "LICENSE", "LICENSE-BRAND",
    "hosted", ".venv", "uv.lock.lock",
}
MUST_BE_IN_SERVICES_APP = {
    "pyproject.toml", "uv.lock", "LICENSE", "LICENSE-BRAND", "hosted", ".venv",
}

# The context probe. A directory planted INSIDE an admitted tree, holding one
# innocent file, seven secrets the ignore file refuses, and three it does not.
# See the test for why all three groups are needed.
CONTEXT_PROBE_DIR = Path("waku") / "_c1_context_probe"
CONTEXT_PROBE_KEPT = "README.txt"

# Refused by `**/.*` (every dotfile) and by `**/*.pem` / `**/*.key`. The
# dotfile half is deliberately broad: an earlier version of this tuple named
# .env and .env.local only, and a review planted .netrc and .aws beside them
# and walked through. These are a sample of a rule, not the rule.
CONTEXT_PROBE_REFUSED = (
    ".env", ".env.local", ".netrc", ".npmrc", ".aws",
    "deploy.pem", "deploy.key",
)

# NOT refused, and pinned here so nobody reads the tuple above as a closed
# class. None of these is dot-prefixed or ends .pem or .key, so each one rides
# into the image inside the tree that admits it. That is a stated limit, not an
# oversight: no pattern tells a secret from a config file by its name, and
# there is no allowlist version of this rule, because whole trees are what
# tenant.Dockerfile.dockerignore admits. If a later change DOES refuse one of
# these, this test goes red and the name moves out of this tuple -- which is
# the right way round: the limit is visible, and shrinking it is a deliberate
# edit rather than a silent one.
CONTEXT_PROBE_GETS_THROUGH = ("id_rsa", "credentials.json", "server.p12")

CONTEXT_PROBE_TAG = "waku-tenant:c1-context-probe"

FAKE_UPSTREAM = r'''
import json, http.server
BODY = {
    "id": "msg_test", "type": "message", "role": "assistant",
    "model": "waku-test-model",
    "content": [{"type": "text", "text": "acknowledged"}],
    "stop_reason": "end_turn", "stop_sequence": None,
    "usage": {"input_tokens": 11, "output_tokens": 3,
              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
}
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        raw = json.dumps(BODY).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def do_GET(self):
        self.send_response(200); self.send_header("content-length", "2")
        self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a):
        pass
http.server.ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
'''

TENANT_CONTAINER = "waku-c1-tenant"
UPSTREAM_CONTAINER = "waku-c1-upstream"
NETWORK = "waku-c1-net"


def _post(container: str, host: str, port: int, path: str, payload: dict) -> dict:
    """One JSON POST from inside `container`, with the stdlib only."""
    program = (
        "import json, urllib.request\n"
        f"body = json.dumps({payload!r}).encode()\n"
        f"req = urllib.request.Request('http://{host}:{port}{path}', data=body,\n"
        "        headers={'content-type': 'application/json'}, method='POST')\n"
        "print(urllib.request.urlopen(req, timeout=120).read().decode())\n"
    )
    proc = dockerlib.exec_in(container, ["python", "-c", program])
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _turn_save_query(container: str) -> tuple[dict, dict, dict]:
    """The three writes, performed HERE rather than assumed to have happened.

    Both fixtures below are function-scoped, so each test gets its own
    container and its own tmpdir. An earlier version of this file had two
    tests whose docstrings claimed "the turn above" and "the settings save
    above" -- there was no above, and one of them could not pass on any
    machine. Any test that needs the three writes calls this.
    """
    turn = _post(container, "127.0.0.1", 7777, "/api/chat",
                 {"message": "hello from the C1 image test"})
    # A settings save writes WAKU_GRAPH_WORKFLOWS into the .env that
    # find_dotenv(usecwd=True) resolves -- /work/.env, because WORKDIR is /work.
    saved = _post(container, "127.0.0.1", 7777, "/api/settings",
                  {"graph_workflows": "1"})
    # The SQL console opens /data/state.db read-only; the turn above created it.
    queried = _post(container, "127.0.0.1", 7777, "/api/query",
                    {"sql": "SELECT count(*) FROM sqlite_master"})
    return turn, saved, queried


def test_the_tenant_image_holds_what_it_copies_and_nothing_more(tenant_image):
    """Reads the BUILT IMAGE, not the ignore file.

    WHAT THIS DOES AND DOES NOT COVER. tenant.Dockerfile COPYs named paths, so
    /app's listing reflects the COPY list, NOT whether the allowlist was read:
    delete tenant.Dockerfile.dockerignore entirely and this test still passes
    while the whole checkout goes to the daemon. The test that notices THAT is
    test_a_secret_planted_inside_an_admitted_tree_stays_out_of_the_image,
    below, which is the guard for the allowlist itself.

    So what this one is for is the COPY list: a new COPY line that drags in a
    directory nobody meant to ship, and the reverse -- a COPY line deleted,
    which a negation-only listing test reads as a pass.
    """
    out = dockerlib.run_once(tenant_image, ["ls", "-a", "/app"], read_only=False).stdout
    entries = set(out.split())

    missing = MUST_BE_IN_APP - entries
    assert not missing, (
        f"the tenant image is missing {sorted(missing)} from /app.\n"
        f"Listing was: {sorted(entries)}. An empty or short listing is how a "
        "deleted COPY line, or a container that printed nothing, looks -- and "
        "without this assertion both read as 'no leaks'.")

    leaked = entries - ADMITTED_IN_APP
    assert not leaked, (
        f"the tenant image holds files its Dockerfile does not COPY: {sorted(leaked)}\n"
        "Either a COPY line was added without adding the entry here, or "
        "tenant.Dockerfile.dockerignore stopped excluding something.")
    assert ".env" not in entries, "a .env from the checkout entered the tenant image"
    assert "hosted" not in entries, (
        "hosted/ is in the tenant image. The two never meet except over HTTP, "
        "and evals/deterministic/test_hosted_boundary.py cannot see a COPY line.")


def test_the_services_image_holds_no_waku(services_image):
    out = dockerlib.run_once(services_image, ["ls", "-a", "/app"],
                             user="0:0", read_only=False).stdout
    entries = set(out.split())

    missing = MUST_BE_IN_SERVICES_APP - entries
    assert not missing, (
        f"the services image is missing {sorted(missing)} from /app.\n"
        f"Listing was: {sorted(entries)}. Without this, the two assertions "
        "below pass against an image that holds nothing at all.")

    assert "waku" not in entries, (
        "waku/ is in the services image. Its allowlist does not admit waku/, so "
        "this means somebody widened services.Dockerfile.dockerignore -- and "
        "with waku/ in the context a `RUN python -c \"import waku\"` starts "
        "working, which is the half of the import boundary the *.py test "
        "cannot see.")
    assert "README.md" not in entries, (
        "README.md is in the services image. Its allowlist does not admit it, "
        "and --no-install-project in services.Dockerfile is there BECAUSE it "
        "is not admitted; if it arrived, the two have drifted apart.")
    assert ".env" not in entries

    leaked = entries - ADMITTED_IN_SERVICES_APP
    assert not leaked, (
        f"the services image holds files its Dockerfile does not COPY: {sorted(leaked)}")


@pytest.fixture()
def planted_context_probe():
    """A directory inside an ADMITTED tree, holding one innocent file, seven
    secrets the ignore file refuses and three it does not.

    THE WHOLE DIRECTORY IS GITIGNORED (`.gitignore`, `waku/_c1_context_probe/`)
    and that line is not tidiness. This fixture writes files called deploy.pem
    and deploy.key into the checkout of a public repository. If a run is killed
    between the mkdir and the finally, the next person to type `git add -A`
    would stage two files named like real private keys, and nobody reads that
    diff twice. Cleanup handles the normal case; the .gitignore line handles
    the abnormal one, and evals/deterministic/hosted/test_image_context.py::
    test_the_context_probes_leftovers_cannot_be_committed checks every name
    this fixture plants against `git check-ignore`, not just the ones somebody
    remembered.
    """
    directory = dockerlib.REPO / CONTEXT_PROBE_DIR
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)
    (directory / CONTEXT_PROBE_KEPT).write_text(
        "Planted by C1's context probe. If you are reading this in a checkout, "
        "a test died before its cleanup ran; delete this directory.\n",
        encoding="utf-8")
    for name in CONTEXT_PROBE_REFUSED + CONTEXT_PROBE_GETS_THROUGH:
        if name == ".aws":
            # A directory, not a file: `**/.*` has to refuse a dot-prefixed
            # DIRECTORY too, and ~/.aws copied into a checkout is the shape
            # the tenant ignore file's own header comment names.
            (directory / name).mkdir()
            (directory / name / "credentials").write_text(
                "C1-CONTEXT-PROBE-NOT-A-REAL-SECRET\n", encoding="utf-8")
            continue
        (directory / name).write_text("C1-CONTEXT-PROBE-NOT-A-REAL-SECRET\n",
                                      encoding="utf-8")
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)
        dockerlib.remove_image(CONTEXT_PROBE_TAG)


def test_a_secret_planted_inside_an_admitted_tree_stays_out_of_the_image(
        tenant_image, planted_context_probe):
    """THE GUARD for the allowlist, and the only test here that fails if
    tenant.Dockerfile.dockerignore is deleted, neutered or never read.

    The allowlist admits whole trees -- `!waku/`, `!skills/` -- so nothing in
    the top-level listing can tell you whether a secret INSIDE one of them got
    in. This plants exactly that: waku/_c1_context_probe/ with a README.txt,
    seven secrets the ignore file refuses, and three it does not.

    WHAT IT PROVES, in three assertions, none of which is enough alone:

      README.txt MUST be there -- otherwise the whole probe directory failed to
      reach the image (a stale cache, a typo in the path, a COPY that does not
      cover it) and every absence below proves nothing. This is the presence
      assertion that stops the test passing vacuously.

      CONTEXT_PROBE_REFUSED must be absent -- `**/.*`, `**/*.pem` and
      `**/*.key`, below the `!` admissions where last-match-wins makes them
      count. `.netrc`, `.npmrc` and the `.aws` DIRECTORY are in that tuple
      because a review planted exactly those against an earlier version that
      named only `.env` and `.env.*`, and got seven green tests.

      the listing must EQUAL README.txt plus CONTEXT_PROBE_GETS_THROUGH --
      no more and no less. This is the honest half. id_rsa, credentials.json
      and server.p12 DO enter the image, because nothing here can tell a
      secret from a config file by its name and there is no allowlist version
      of this rule: whole trees are what the ignore file admits. Pinning the
      survivors exactly means the limit is written down and visible, a NEW
      shape getting through turns this red, and tightening the rule later also
      turns it red -- which is correct, because that is an edit to a stated
      security boundary and it should not happen quietly.

    Cost note, which is why this is affordable in C4: when the re-exclusions
    work, the refused files never enter the context, so the COPY layer's hash
    changes only by the four survivors, and this build is close to a cache hit
    on top of the session's tenant_image.
    """
    tag = dockerlib.build_image("hosted/image/tenant.Dockerfile", CONTEXT_PROBE_TAG)
    inside = f"/app/{CONTEXT_PROBE_DIR.as_posix()}"
    proc = dockerlib.run_once(tag, ["ls", "-a", inside], read_only=False, check=False)

    assert proc.returncode == 0, (
        f"{inside} is not in the image at all, so this test proves nothing "
        "about the re-exclusions -- it would 'pass' for a probe that never "
        f"arrived. ls said: {proc.stderr[-500:]}")

    entries = set(proc.stdout.split())
    assert CONTEXT_PROBE_KEPT in entries, (
        f"the probe directory is in the image but {CONTEXT_PROBE_KEPT} is not "
        f"({sorted(entries)}), so the build did not see the planted files and "
        "every assertion below is vacuous.")

    leaked = entries & set(CONTEXT_PROBE_REFUSED)
    assert not leaked, (
        f"secret-shaped files inside an admitted tree entered the tenant "
        f"image: {sorted(leaked)}\n"
        "tenant.Dockerfile.dockerignore's `**/.*`, `**/*.pem` and `**/*.key` "
        "lines are what stop this, and they only count while they sit BELOW "
        "the `!` admissions -- Docker is last-match-wins. A real waku/.env "
        "reaches the daemon and the image the same way.")

    expected = {".", "..", CONTEXT_PROBE_KEPT, *CONTEXT_PROBE_GETS_THROUGH}
    assert entries == expected, (
        f"the surviving listing is {sorted(entries)}, this test expects "
        f"{sorted(expected)}.\n"
        "If something NEW survived: a secret shape the ignore file does not "
        "cover just entered the image, and the question is whether the rule "
        "can grow or whether the limit has to be stated more loudly.\n"
        "If something EXPECTED is now missing: somebody tightened the rule, "
        "which is good -- move that name out of CONTEXT_PROBE_GETS_THROUGH in "
        "the same commit, so the stated limit and the real one stay the same "
        "sentence.")


def test_xfsprogs_sqlite3_and_zstd_are_in_the_services_image(services_image):
    """The spawner runs all three (spec, "The spawner"). Checked by running
    them, not by reading the apt line: a package that installs and puts its
    binary somewhere unexpected passes a text check and fails in production."""
    for binary, args in (("xfs_quota", ["-V"]), ("sqlite3", ["-version"]),
                         ("zstd", ["--version"])):
        proc = dockerlib.run_once(services_image, [binary, *args],
                                  user="0:0", read_only=False, check=False)
        assert proc.returncode == 0, f"{binary} is not runnable: {proc.stderr[-500:]}"


@pytest.fixture()
def tenant_dirs_on_host(tenant_image):
    """The two host directories a tenant container mounts, handed to 10001."""
    base = Path(tempfile.mkdtemp(prefix="waku-c1-"))
    home, env = base / "home", base / "env"
    home.mkdir()
    env.mkdir()
    # What C2's provisioning will write. Written here through
    # core/provision.render_env() rather than by hand, so C1's test and C2's
    # spawner cannot disagree about the one line a tenant's .env carries.
    (env / ".env").write_text(render_env(), encoding="utf-8")
    dockerlib.chown_to_tenant(base, tenant_image)
    yield home, env
    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture()
def running_tenant(tenant_image, tenant_dirs_on_host):
    """A tenant container and a fake upstream, on their own network.

    The read-only root, the tmpfs and the two mounts are the spec's template --
    and only those three. CapDrop, no-new-privileges, seccomp and the resource
    limits are C2's, and a container started here has none of them: this test
    is about the IMAGE, so it keeps dockerlib.start_detached as the runner.

    The ENVIRONMENT now comes from hosted/spawner/template.py. C1 wrote the
    nine variables out by hand because C2 did not exist; from C2 on there is
    one source, so the image test and the spawner cannot drift apart about
    what a tenant container is started with.
    """
    home, env = tenant_dirs_on_host
    # Pre-emptive, not paranoid: a run killed mid-fixture leaves these two
    # fixed names behind, and every later run then dies at `docker run --name`
    # with a message about a name conflict rather than about the test. The
    # network cannot be removed while a stale container still holds it, so the
    # containers go first.
    for name in (TENANT_CONTAINER, UPSTREAM_CONTAINER):
        dockerlib.remove(name)
    dockerlib.network_remove(NETWORK)
    dockerlib.network_create(NETWORK, "--driver", "bridge")
    try:
        dockerlib.start_detached(
            tenant_image, ["python", "-c", FAKE_UPSTREAM],
            name=UPSTREAM_CONTAINER, network=NETWORK, read_only=False)
        # The upstream is a probe TARGET, so the plan's own rule applies to it:
        # assert it came up before anything depends on its answers. Without
        # this, a fake server that died on start surfaces as `assert "error"
        # not in turn` -- loud, but diagnosed as a broken turn.
        dockerlib.assert_alive(UPSTREAM_CONTAINER)
        dockerlib.wait_for_listener(UPSTREAM_CONTAINER, "127.0.0.1", 8080)

        body = template.tenant_container(
            _config_for_tests(tenant_image,
                              upstream_url=f"http://{UPSTREAM_CONTAINER}:8080"),
            # A tenant id is twelve characters of [a-z2-7]; the brief's
            # "c1testc1test" carries a `1`, which tenant_container refuses.
            tenant_id="ctestctestct", project_id=2, timezone="UTC",
            token="x" * 43)
        tenant = dockerlib.start_detached(
            tenant_image, name=TENANT_CONTAINER, network=NETWORK,
            binds=[f"{home}:/data", f"{env}:/work"],
            env=dict(entry.split("=", 1) for entry in body["Env"]))
        dockerlib.assert_alive(tenant)
        dockerlib.wait_for_listener(tenant, "127.0.0.1", 7777)
        yield tenant
    finally:
        for name in (TENANT_CONTAINER, UPSTREAM_CONTAINER):
            dockerlib.remove(name)
        dockerlib.network_remove(NETWORK)


def test_a_turn_a_settings_save_and_a_sql_query_all_work_under_a_read_only_root(running_tenant):
    """Design section 10.6: the container writes nowhere but /data, /work and
    /tmp. Proved by doing the three things that write to each of them with the
    root filesystem read-only, not by listing mounts."""
    turn, saved, queried = _turn_save_query(running_tenant)

    assert "error" not in turn, f"the turn failed: {turn}"
    assert turn.get("reply"), f"the turn returned no reply: {turn}"
    assert "error" not in saved, f"the settings save failed: {saved}"
    assert "error" not in queried, f"the SQL console failed: {queried}"
    assert queried["rows"], f"the SQL console returned no rows: {queried}"


def test_the_writable_layer_is_untouched_after_a_turn_a_save_and_a_query(running_tenant):
    """`docker diff` lists what changed in the container's own layer. Bind
    mounts and tmpfs are not in it, so after a turn, a save and a query this is
    empty -- everything waku wrote went to /data, /work or /tmp.

    The three writes happen HERE. The fixture is function-scoped, so an earlier
    test's container is not this one, and a version of this test that only said
    so in its docstring was proving something much weaker: that a freshly
    booted dashboard writes nothing.
    """
    turn, saved, queried = _turn_save_query(running_tenant)
    assert "error" not in turn, f"the turn failed before the diff: {turn}"
    assert "error" not in saved, f"the save failed before the diff: {saved}"
    assert "error" not in queried, f"the query failed before the diff: {queried}"

    changed = dockerlib.diff(running_tenant)
    assert not changed, (
        f"the tenant container wrote to its own layer: {changed}\n"
        "Everything a tenant writes belongs in /data, /work or /tmp.")


def test_the_env_the_tenant_reads_and_writes_is_the_one_in_work(running_tenant,
                                                                tenant_dirs_on_host):
    """WORKDIR is /work so waku's find_dotenv(usecwd=True) (waku/config.py:35)
    finds the tenant's own .env and no other.

    This test performs the save ITSELF and reads the host side of the mount
    before and after. An earlier version asserted that a save done in a
    DIFFERENT test's container had landed here; both fixtures are
    function-scoped, so it could not pass on any machine, and its failure
    message blamed find_dotenv for what was a fixture-scope bug.

    The before-assertion is what makes the after-assertion mean something: it
    proves WAKU_GRAPH_WORKFLOWS was not already in the file that render_env()
    wrote, so its presence afterwards is this save and nothing else.
    """
    _, env = tenant_dirs_on_host
    dotenv = env / ".env"

    before = dotenv.read_text(encoding="utf-8")
    assert "WAKU_PROVIDER=waku-platform" in before, (
        f"the provisioned .env is not what render_env() writes: {before!r}")
    assert "WAKU_GRAPH_WORKFLOWS" not in before, (
        "WAKU_GRAPH_WORKFLOWS is already in the provisioned .env, so the "
        f"assertion below would pass without any save happening: {before!r}")

    saved = _post(running_tenant, "127.0.0.1", 7777, "/api/settings",
                  {"graph_workflows": "1"})
    assert "error" not in saved, f"the settings save failed: {saved}"

    after = dotenv.read_text(encoding="utf-8")
    assert "WAKU_GRAPH_WORKFLOWS" in after, (
        "the settings save did not land in /work/.env, so find_dotenv found "
        f"some other file -- or none, and set_key wrote a new one elsewhere. "
        f"The file still reads: {after!r}")
    assert "WAKU_PROVIDER=waku-platform" in after, (
        "the settings save replaced the provisioned .env instead of editing "
        f"it, and WAKU_PROVIDER is gone: {after!r}")
