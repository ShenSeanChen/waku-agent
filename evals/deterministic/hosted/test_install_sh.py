"""checks.sh's six functions and install.sh's refusals, offline.

WHAT THIS FILE CANNOT DO: it never creates a directory owned by UID 10002,
never starts a container and never asks a daemon anything. The install's
effects are evals/hosted_docker/test_compose.py's, and the whole install on a
real VM is recorded check F1-R1. What is HERE is every decision install.sh
makes before it touches the machine -- which is where an installer goes wrong
quietly.

WHERE THE LINE FALLS, AND WHY IT FALLS THERE. install.sh parses its flags,
checks that every required one was given and checks the domain's shape, and
only then calls waku_require_root and reads /etc/os-release. Everything above
that line is reachable from a maintainer's laptop and is tested here.
Everything below it -- the XFS refusal, the two Supabase refusals, the
firewall unit's two paths, the proxy's scale rule -- needs root, /proc and an
Ubuntu release, so it belongs to recorded check F1-R1 and is written up there
with a result block. Moving waku_require_root above the parser to "fix" that
would make a script that demands root before telling you a flag is misspelt,
which is a worse script; faking it away behind an environment variable would
put a hole in the one guard that keeps this installer from half-running as a
normal user.
"""

from __future__ import annotations

import json
import os

import pytest
import shelllib

CHECKS = shelllib.DEPLOY / "checks.sh"
INSTALL = shelllib.DEPLOY / "install.sh"

# Stubs for the four commands install.sh would otherwise reach for. Every
# refusal below happens before any of them is called, so the call log is
# expected to be empty -- the stubs exist so that a regression which lets the
# script run on has something to record instead of touching the machine.
OUTSIDE = ["apt-get", "systemctl", "docker", "curl", "git"]


def _key_file(tmp_path, text: str = "sk-ant-installtest\n"):
    path = tmp_path / "platform-key"
    path.write_text(text, encoding="utf-8")
    return path


def _required(tmp_path, domain: str = "example.test", key=None) -> list[str]:
    """Every required flag with a plausible value, so a test that is about one
    refusal is not accidentally about a missing flag."""
    key = _key_file(tmp_path) if key is None else key
    return [domain,
            "--dns-provider", "route53",
            "--acme-email", "a@b.test",
            "--data-device", "/dev/null",
            "--free-model", "claude-haiku-4-5",
            "--platform-key-file", str(key),
            "--supabase-url", "https://p.supabase.co",
            "--supabase-publishable-key", "sb_publishable_x",
            "--supabase-audience", "https://api.waku.one/mcp"]


# --- checks.sh ---------------------------------------------------------------


def test_sixteen_gigabytes_of_memory_gives_ninety_five_running_tenants(tmp_path):
    """The spec's formula and the spec's own worked answer: "(the VM's memory
    minus 2 GB, divided by 150 MB), which is about 95 on design section 15's
    16 GB VM".

    THE 95 IS A LITERAL HERE, not a re-computation of the formula in the
    script. A test that recomputed it would agree with any formula, including
    one that divided by the wrong number.
    """
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       16777216 kB\nMemFree: 1 kB\n", encoding="utf-8")
    done = shelllib.call_function(CHECKS, f'waku_max_running "{meminfo}"')
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "95"


def test_a_small_vm_still_gets_one_running_tenant(tmp_path):
    """2 GB of memory makes the numerator zero, and a cap of 0 would refuse
    every tenant on a VM that can clearly run one."""
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:        2097152 kB\n", encoding="utf-8")
    done = shelllib.call_function(CHECKS, f'waku_max_running "{meminfo}"')
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "1"


def test_meminfo_without_a_total_is_refused(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemFree:  1024 kB\n", encoding="utf-8")
    done = shelllib.call_function(CHECKS, f'waku_max_running "{meminfo}"')
    assert done.returncode != 0


def test_ubuntus_stub_resolver_alone_is_refused(tmp_path):
    """/etc/resolv.conf on Ubuntu 24.04 names only 127.0.0.53. Opening a
    firewall rule to a loopback stub lets nothing through and looks as though
    it had, which is why install.sh reads the systemd file instead."""
    stub = tmp_path / "stub-resolv.conf"
    stub.write_text("nameserver 127.0.0.53\noptions edns0 trust-ad\n", encoding="utf-8")
    done = shelllib.call_function(CHECKS, f'waku_resolvers "{stub}"')
    assert done.returncode != 0
    assert done.stdout.strip() == ""


def test_a_resolv_conf_with_no_nameserver_at_all_is_refused(tmp_path):
    """The empty-source case `pipefail` cannot catch on its own: awk over a
    file with no matching line succeeds and prints nothing, so without the
    trailing `grep .` this function would hand install.sh an empty allow-list
    and exit 0."""
    empty = tmp_path / "resolv.conf"
    empty.write_text("search example.test\n", encoding="utf-8")
    done = shelllib.call_function(CHECKS, f'waku_resolvers "{empty}"')
    assert done.returncode != 0
    assert done.stdout.strip() == ""


def test_the_real_resolvers_come_back_comma_separated(tmp_path):
    real = tmp_path / "resolv.conf"
    real.write_text(
        "nameserver 10.0.0.2\nnameserver 127.0.0.53\nnameserver 1.1.1.1\n",
        encoding="utf-8")
    done = shelllib.call_function(CHECKS, f'waku_resolvers "{real}"')
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "10.0.0.2,1.1.1.1"


@pytest.mark.parametrize(
    ("line", "ok"),
    [
        ("/dev/sdb1 /srv/waku xfs rw,relatime,attr2,prjquota 0 0", True),
        # Mounted, and enforcing nothing: xfs_quota accepts every command on
        # such a filesystem and applies none of it.
        ("/dev/sdb1 /srv/waku xfs rw,relatime,attr2 0 0", False),
        # The right options on the wrong filesystem.
        ("/dev/sdb1 /srv/waku ext4 rw,relatime,prjquota 0 0", False),
        # The right filesystem at the wrong place.
        ("/dev/sdb1 /srv/other xfs rw,prjquota 0 0", False),
        # A superstring of the option, which a naive grep would accept and
        # which means the opposite of what it contains.
        ("/dev/sdb1 /srv/waku xfs rw,noprjquota 0 0", False),
        # The mount point as a prefix of the wanted one.
        ("/dev/sdb1 /srv/waku-old xfs rw,prjquota 0 0", False),
    ])
def test_the_xfs_verdict_is_a_closed_set(tmp_path, line, ok):
    mounts = tmp_path / "mounts"
    mounts.write_text(f"proc /proc proc rw 0 0\n{line}\n", encoding="utf-8")
    done = shelllib.call_function(
        CHECKS, f'waku_xfs_prjquota_ok "{mounts}" /srv/waku')
    assert (done.returncode == 0) is ok


@pytest.mark.parametrize(
    ("document", "ok"),
    [
        ({"keys": [{"kty": "EC", "alg": "ES256"}]}, True),
        ({"keys": [{"kty": "RSA"}, {"kty": "EC"}]}, True),
        # HS256: a shared secret. The gateway would hold the key that MINTS
        # tokens, so every leak of config/gateway.env becomes a sign-in as
        # anyone.
        ({"keys": [{"kty": "oct", "alg": "HS256"}]}, False),
        ({"keys": [{"kty": "EC"}, {"kty": "oct"}]}, False),
        ({"keys": []}, False),
        # Default-deny: a document this check cannot read is a refusal, not a
        # shrug.
        ({}, False),
        ({"keys": "EC"}, False),
        ({"keys": [{"alg": "ES256"}]}, False),
    ])
def test_only_an_all_asymmetric_jwks_passes(tmp_path, document, ok):
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    done = shelllib.call_function(CHECKS, f'waku_jwks_is_asymmetric "{path}"')
    assert (done.returncode == 0) is ok


@pytest.mark.parametrize(
    ("document", "ok"),
    [
        ({"disable_signup": True}, True),
        ({"disable_signup": False}, False),
        # The field renamed or dropped by a Supabase release: refused, because
        # "I could not tell" must not read as "signup is closed".
        ({"external": {"email": True}}, False),
        ({"disable_signup": "true"}, False),
        ({"disable_signup": 1}, False),
    ])
def test_only_a_project_with_signup_off_passes(tmp_path, document, ok):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    done = shelllib.call_function(CHECKS, f'waku_signup_is_closed "{path}"')
    assert (done.returncode == 0) is ok


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1G", "1073741824"), ("2G", "2147483648"), ("512M", "536870912"),
     ("1024K", "1048576"), ("1073741824", "1073741824")])
def test_a_disk_size_becomes_bytes(text, expected):
    done = shelllib.call_function(CHECKS, f"waku_bytes {text}")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == expected


@pytest.mark.parametrize("text", ["1T", "1 G", "-1G", "G", "", "1.5G", "1GB", "0x10"])
def test_a_disk_size_that_is_not_a_number_and_a_unit_is_refused(text):
    done = shelllib.call_function(CHECKS, f'waku_bytes "{text}"')
    assert done.returncode != 0


# --- install.sh's argument refusals -----------------------------------------


def test_an_unknown_flag_is_refused_and_nothing_runs(tmp_path):
    """A closed set. An ignored flag is how an operator comes to believe they
    set a value they did not -- and --tenant-disc for --tenant-disk would leave
    every tenant on the default while the operator thought otherwise."""
    done = shelllib.run(INSTALL, ["example.test", "--tenant-disc", "2G"],
                        tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert "unknown flag: --tenant-disc" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_a_missing_required_flag_is_refused(tmp_path):
    done = shelllib.run(INSTALL, ["example.test", "--dns-provider", "route53"],
                        tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert "--acme-email is required" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_a_second_domain_is_refused(tmp_path):
    """Same reason as the unknown flag: an operator who typed the apex twice,
    or who put a value where a flag was expected, must be told rather than
    have one of the two silently win."""
    args = _required(tmp_path) + ["second.test"]
    done = shelllib.run(INSTALL, args, tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert "two domains given: example.test and second.test" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_a_domain_with_no_dot_is_refused(tmp_path):
    """A tenant host is <id>.<domain>, and <id> is a DNS label. A single-label
    apex cannot carry a wildcard certificate, so this fails at ACME time --
    after the VM is built -- unless it fails here."""
    done = shelllib.run(INSTALL, _required(tmp_path, domain="localhost"),
                        tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert "must be a hostname with a dot" in done.stderr
    assert shelllib.calls(tmp_path) == []


# --- install.sh's platform key, which is read from a file and never taken as
# --- a command-line value (ruled 2026-09-25)


def test_a_platform_key_file_that_is_not_there_is_refused(tmp_path):
    missing = tmp_path / "no-such-key"
    done = shelllib.run(INSTALL, _required(tmp_path, key=missing),
                        tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert f"--platform-key-file: no such file: {missing}" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_an_unreadable_platform_key_file_is_refused(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can read a 0000 file, so this refusal is unreachable as root")
    key = _key_file(tmp_path)
    key.chmod(0o000)
    done = shelllib.run(INSTALL, _required(tmp_path, key=key),
                        tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert f"--platform-key-file: cannot read {key}" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_an_empty_platform_key_file_is_refused(tmp_path):
    """The whole reason the file is read here rather than trusted: an empty
    file would otherwise write WAKU_PLATFORM_KEY= into proxy.env and fail at
    the first model call, days later, with nothing pointing back here."""
    key = _key_file(tmp_path, "\n")
    done = shelllib.run(INSTALL, _required(tmp_path, key=key),
                        tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert f"--platform-key-file: {key} is empty" in done.stderr
    assert shelllib.calls(tmp_path) == []


@pytest.mark.parametrize(
    "text",
    [
        # An embedded newline would split proxy.env's KEY=VALUE line in two,
        # and the proxy would read the second half as a setting of its own.
        "sk-ant-one\nsk-ant-two\n",
        # A leading blank line does the same thing from the other end.
        "\nsk-ant-abc\n",
        # Whitespace either side is carried straight into the Authorization
        # header, where it fails as an authentication error rather than as a
        # configuration one.
        "  sk-ant-abc\n",
        "sk-ant-abc \n",
        "sk-ant abc\n",
        "sk-ant-abc\t\n",
    ])
def test_a_platform_key_file_holding_anything_but_the_key_is_refused(tmp_path, text):
    """A CLOSED SET: printable, non-space characters only. Naming the ways a
    file can be wrong would miss the next one; naming what is allowed does
    not."""
    key = _key_file(tmp_path, text)
    done = shelllib.run(INSTALL, _required(tmp_path, key=key),
                        tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert "must hold the key and nothing else" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_the_editors_trailing_newline_is_not_part_of_the_key(tmp_path):
    """Every editor ends a file with a newline, so a key file that has one is
    the common case and must be accepted.

    The assertion is that the run gets PAST the key refusals: with the newline
    still attached the value would carry a non-printable character and the
    closed-set check above would refuse it, so "no key refusal" is exactly the
    evidence that the newline was removed. The run still fails -- the next
    thing install.sh does is demand root -- and that is the point: this test
    is about which refusal, not about whether.
    """
    key = _key_file(tmp_path, "sk-ant-abc\n")
    done = shelllib.run(INSTALL, _required(tmp_path, key=key),
                        tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert "--platform-key-file" not in done.stderr, done.stderr
    assert "must hold the key and nothing else" not in done.stderr
