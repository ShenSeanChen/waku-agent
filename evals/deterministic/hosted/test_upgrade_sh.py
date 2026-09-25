"""upgrade.sh's two refusals and its order.

WHAT CAN GO WRONG HERE IS ORDER, not shell. An upgrade that restarts the
services before rebuilding the images restarts them onto the old ones; an
upgrade that runs restart-all before the gateway answers again restarts every
tenant through a gateway that is not there. Both look like success.
"""

from __future__ import annotations

import shelllib

UPGRADE = shelllib.DEPLOY / "upgrade.sh"

_GIT_DIRTY = """#!/bin/sh
printf '%s %s\\n' git "$*" >> "$WAKU_CALLS"
case "$*" in
  *"status --porcelain"*) echo " M waku/loop/agent.py" ;;
  *"rev-parse"*) echo 0000000000000000000000000000000000000000 ;;
esac
exit 0
"""

_GIT_CLEAN = """#!/bin/sh
printf '%s %s\\n' git "$*" >> "$WAKU_CALLS"
case "$*" in
  *"status --porcelain"*) : ;;
  *"rev-parse"*) echo 0000000000000000000000000000000000000000 ;;
esac
exit 0
"""


def _install_env(tmp_path):
    src = tmp_path / "src"
    (src / "hosted" / "image").mkdir(parents=True)
    build = src / "hosted" / "image" / "build.sh"
    build.write_text('#!/bin/sh\nprintf "%s %s\\n" build.sh "$*" >> "$WAKU_CALLS"\n',
                     encoding="utf-8")
    build.chmod(0o755)
    env_file = tmp_path / "install.env"
    env_file.write_text(
        f"WAKU_ROOT={tmp_path}/waku\nWAKU_SRC={src}\n"
        f"WAKU_COMPOSE={src}/hosted/deploy/compose.yaml\n"
        "WAKU_DOMAIN=example.test\nWAKU_DNS_PROVIDER=route53\n"
        "WAKU_GATEWAY_ADDRESS=127.0.0.1:8787\n"
        "WAKU_TENANT_IMAGE=waku-tenant:current\n"
        "WAKU_SERVICES_IMAGE=waku-services:current\n"
        "WAKU_CADDY_IMAGE=waku-caddy:current\n",
        encoding="utf-8")
    return {"WAKU_INSTALL_ENV": str(env_file)}


def test_a_dirty_checkout_is_refused_before_anything_is_built(tmp_path):
    done = shelllib.run(UPGRADE, [], tmp_path=tmp_path,
                        env=_install_env(tmp_path),
                        stubs=["git", "docker", "curl", "id"],
                        bodies={"git": _GIT_DIRTY,
                                "id": '#!/bin/sh\necho 0\n'})
    assert done.returncode != 0
    assert "uncommitted changes" in done.stderr
    assert not [line for line in shelllib.calls(tmp_path)
                if line.startswith(("docker", "build.sh"))]


def test_the_images_are_rebuilt_before_the_services_restart(tmp_path):
    done = shelllib.run(UPGRADE, [], tmp_path=tmp_path,
                        env=_install_env(tmp_path),
                        stubs=["git", "docker", "curl", "id"],
                        bodies={"git": _GIT_CLEAN,
                                "id": '#!/bin/sh\necho 0\n'})
    assert done.returncode == 0, done.stderr
    calls = shelllib.calls(tmp_path)
    built = next(i for i, line in enumerate(calls) if line.startswith("build.sh"))
    restarted = next(i for i, line in enumerate(calls) if " up -d" in line)
    assert built < restarted


def test_restart_all_runs_only_after_the_gateway_answers(tmp_path):
    """--now restarts every tenant through the running gateway. If it ran
    before the readiness probe, an upgrade that left the gateway down would
    report success having restarted nobody."""
    done = shelllib.run(UPGRADE, ["--now"], tmp_path=tmp_path,
                        env=_install_env(tmp_path),
                        stubs=["git", "docker", "curl", "id"],
                        bodies={"git": _GIT_CLEAN,
                                "id": '#!/bin/sh\necho 0\n'})
    assert done.returncode == 0, done.stderr
    calls = shelllib.calls(tmp_path)
    probed = next(i for i, line in enumerate(calls) if line.startswith("curl"))
    restarted = next(i for i, line in enumerate(calls) if "restart-all" in line)
    assert probed < restarted


def test_an_unknown_argument_is_refused(tmp_path):
    done = shelllib.run(UPGRADE, ["--force"], tmp_path=tmp_path,
                        env=_install_env(tmp_path), stubs=["git", "docker", "curl"])
    assert done.returncode != 0
    assert "unknown argument: --force" in done.stderr
