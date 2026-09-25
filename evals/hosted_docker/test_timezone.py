"""Acceptance 4 -- a tenant's assistant uses the tenant's time zone.

waku/runtime/session.py writes the current time into the system prompt from a
NAIVE local datetime, so %Z and %z come from the process's TZ. The assertion is
on the zone's own date and UTC offset, computed here with zoneinfo, never on a
string copied out of that source line.

PARAMETRISED OVER TWO ZONES on purpose. A test that only checks Shanghai
passes on a machine that is already in Shanghai, and on a container whose TZ
was ignored if the runner happens to be at +0800.

The container's ENVIRONMENT comes from hosted/spawner/template.py, not from a
hand-written dict: the whole point of the template is that there is one source
for what a tenant container is started with.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import dockerlib
import pytest
from spawnerlib import TENANT_TAG

from hosted.core.provision import render_env
from hosted.core.tenant import tenant_dirs
from hosted.spawner import template

# Records the last request body to a file on a mount the test reads.
RECORDING_UPSTREAM = r'''
import json, http.server
BODY = {"id": "msg_test", "type": "message", "role": "assistant",
        "model": "waku-test-model",
        "content": [{"type": "text", "text": "acknowledged"}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 3,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        with open("/record/last.json", "wb") as handle:
            handle.write(raw)
        out = json.dumps(BODY).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)
    def log_message(self, *a):
        pass
http.server.ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
'''

UPSTREAM_CONTAINER = "waku-tz-upstream"
TENANT_CONTAINER = "waku-tz-tenant"
NETWORK = "waku-tz-net"
TZ_TENANT_ID = "tztztztztztz"
TZ_PROJECT_ID = 2
TZ_TOKEN = "z" * 43


def _config_for_tests(tenant_root: Path) -> template.SpawnerConfig:
    """A SpawnerConfig pointing at this test's own tree.

    The seccomp profile is read as TEXT by config_from_env; this test does not
    go through the spawner, so it builds the dataclass directly and passes the
    committed profile's contents.
    """
    return template.SpawnerConfig(
        tenant_root=tenant_root,
        archive_root=tenant_root / "archive",
        staging_root=tenant_root / "staging",
        tenant_image=TENANT_TAG,
        services_image="waku-services:test",
        platform_base_url=f"http://{UPSTREAM_CONTAINER}:8080",
        platform_model="waku-test-model",
        platform_small_model="waku-test-model",
        tenant_disk_bytes=64 * 1024 * 1024,
        data_device="none",
        seccomp_profile=(dockerlib.REPO / "hosted" / "image"
                         / "seccomp.json").read_text(encoding="utf-8"),
    )


@pytest.mark.parametrize("zone,offset", [("Asia/Shanghai", "UTC+0800"),
                                         ("UTC", "UTC+0000")])
def test_a_container_started_with_a_zone_puts_that_zone_in_the_system_prompt(
        tenant_image, tmp_path, zone, offset):
    record = tmp_path / "record"
    record.mkdir()
    dockerlib.chown_to_tenant(record, tenant_image)

    # The two directories the template will bind, at the paths tenant_dirs
    # builds, so the environment below and the mounts below come from one call.
    root = tmp_path / "tenants"
    dirs = tenant_dirs(root, TZ_TENANT_ID)
    dirs.home.mkdir(parents=True)
    dirs.env.mkdir(parents=True)
    (dirs.env / ".env").write_text(render_env(), encoding="utf-8")
    dockerlib.chown_to_tenant(tmp_path, tenant_image)

    body = template.tenant_container(
        _config_for_tests(root), tenant_id=TZ_TENANT_ID, project_id=TZ_PROJECT_ID,
        timezone=zone, token=TZ_TOKEN)
    env = dict(entry.split("=", 1) for entry in body["Env"])
    binds = body["HostConfig"]["Binds"]

    for name in (TENANT_CONTAINER, UPSTREAM_CONTAINER):
        dockerlib.remove(name)
    dockerlib.network_remove(NETWORK)
    dockerlib.network_create(NETWORK, "--driver", "bridge")
    try:
        dockerlib.start_detached(
            tenant_image, ["python", "-c", RECORDING_UPSTREAM],
            name=UPSTREAM_CONTAINER, network=NETWORK, read_only=False,
            binds=[f"{record}:/record"])
        dockerlib.assert_alive(UPSTREAM_CONTAINER)
        dockerlib.wait_for_listener(UPSTREAM_CONTAINER, "127.0.0.1", 8080)

        dockerlib.start_detached(
            tenant_image, name=TENANT_CONTAINER, network=NETWORK, binds=binds,
            env=env)
        dockerlib.assert_alive(TENANT_CONTAINER)
        dockerlib.wait_for_listener(TENANT_CONTAINER, "127.0.0.1",
                                    template.DASHBOARD_PORT)
        program = (
            "import json, urllib.request\n"
            "body = json.dumps({'message': 'what day is it'}).encode()\n"
            "req = urllib.request.Request('http://127.0.0.1:7777/api/chat',\n"
            "    data=body, headers={'content-type': 'application/json'},\n"
            "    method='POST')\n"
            "print(urllib.request.urlopen(req, timeout=120).read().decode())\n")
        dockerlib.exec_in(TENANT_CONTAINER, ["python", "-c", program])

        recorded = json.loads((record / "last.json").read_text(encoding="utf-8"))
        system = json.dumps(recorded.get("system", ""))
        assert system and system != '""', (
            "the upstream recorded no system prompt; the turn did not reach it "
            "and nothing below is an assertion about time zones")
        now = datetime.now(ZoneInfo(zone))
        assert f"{now:%Y-%m-%d}" in system, system[:400]
        assert offset in system, system[:400]
    finally:
        for name in (TENANT_CONTAINER, UPSTREAM_CONTAINER):
            dockerlib.remove(name)
        dockerlib.network_remove(NETWORK)
