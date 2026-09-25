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
    path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
    return path


def _dns_file(tmp_path, text: str = "AWS_ACCESS_KEY_ID=AKIAEXAMPLE\n"
                                    "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG+bPxRfiCY\n"
                                    "AWS_REGION=us-east-1\n"):
    path = tmp_path / "dns-env"
    path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
    return path


def _required(tmp_path, domain: str = "example.test", key=None,
              dns_env_file=None) -> list[str]:
    """Every required flag with a plausible value, so a test that is about one
    refusal is not accidentally about a missing flag."""
    key = _key_file(tmp_path) if key is None else key
    dns_env_file = _dns_file(tmp_path) if dns_env_file is None else dns_env_file
    return [domain,
            "--dns-provider", "route53",
            "--acme-email", "a@b.test",
            "--data-device", "/dev/null",
            "--free-model", "claude-haiku-4-5",
            "--platform-key-file", str(key),
            "--dns-env-file", str(dns_env_file),
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
    """A file systemd wrote before it had an answer. awk over it prints
    nothing, and a pipeline over an empty source is the shape `pipefail` cannot
    help with -- it only fails here because each `grep` in the chain exits 1 on
    empty input. An installer that took the empty result would write an empty
    --dns-allow and open DNS to nobody."""
    empty = tmp_path / "resolv.conf"
    empty.write_text("search example.test\n", encoding="utf-8")
    done = shelllib.call_function(CHECKS, f'waku_resolvers "{empty}"')
    assert done.returncode != 0
    assert done.stdout.strip() == ""


def test_a_nameserver_line_with_no_address_is_refused(tmp_path):
    """The one case the two loopback filters let through, and the whole reason
    for the trailing `grep .`: awk prints an empty field, neither `grep -v`
    matches it, and `paste` turns it into one blank line -- a pipeline that
    exits 0 having produced an empty allow-list. Measured: without that last
    `grep .` this fixture exits 0."""
    malformed = tmp_path / "resolv.conf"
    malformed.write_text("nameserver\nsearch example.test\n", encoding="utf-8")
    done = shelllib.call_function(CHECKS, f'waku_resolvers "{malformed}"')
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


@pytest.mark.parametrize("text", ["0", "0G", "0M", "0K"])
def test_a_disk_size_of_zero_is_refused(text):
    """XFS READS bhard=0 AS NO LIMIT. hosted/spawner/xfsquota.limit_argv puts
    WAKU_TENANT_DISK_BYTES straight into `limit -p bhard=<n>`, so
    `--tenant-disk 0` -- which reads as the smallest possible quota -- would
    put every tenant on an unbounded disk, with none of the
    WAKU_DATA_DEVICE=none warnings to say so. That is the outcome the XFS
    preflight refusal exists to prevent, reached through the flag.

    The earlier check rejected non-digit CHARACTERS and so accepted this.
    """
    done = shelllib.call_function(CHECKS, f'waku_bytes "{text}"')
    assert done.returncode != 0


@pytest.mark.parametrize("text", ["010G", "01", "00"])
def test_a_disk_size_with_a_leading_zero_is_refused(text):
    """Not pedantry: bash reads a leading zero as octal, so `010G` would be
    8 GiB to the arithmetic and 10 GiB to the operator."""
    done = shelllib.call_function(CHECKS, f'waku_bytes "{text}"')
    assert done.returncode != 0


@pytest.mark.parametrize(
    "text", ["9999999999999G", "99999999999999999999G", "18446744073709551616"])
def test_a_disk_size_that_would_wrap_is_refused(text):
    """Measured before the fix: 9999999999999G came back as
    1413189099967217664, a perfectly plausible-looking quota an operator would
    never notice. A digit-class check constrains the characters and says
    nothing about the magnitude."""
    done = shelllib.call_function(CHECKS, f'waku_bytes "{text}"')
    assert done.returncode != 0


@pytest.mark.parametrize(
    ("pair", "ok"),
    [
        ("AWS_REGION=us-east-1", True),
        ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", True),
        ("CLOUDFLARE_API_TOKEN=abc-DEF_123", True),
        ("_LEADING_UNDERSCORE=x", True),
        # Measured against a real `docker compose config`: Compose accepts a
        # line with no `=` and leaves the variable UNSET, so the operator comes
        # to believe they set a credential they did not.
        ("AWS_REGION", False),
        ("=value", False),
        ("1BAD=value", False),
        ("AWS REGION=x", False),
        ("AWS-REGION=x", False),
        ("AWS_REGION=", False),
        # A newline writes a second variable into caddy.env that nobody asked
        # for.
        ("A=b\nC=d", False),
        ("A=b c", False),
        ("A=b\t", False),
        ("A=b\r", False),
    ])
def test_one_env_pair_is_a_closed_set(tmp_path, pair, ok):
    script = tmp_path / "call.sh"
    script.write_text(
        f'. "{CHECKS}"\nwaku_env_pair_ok "$1"\n', encoding="utf-8")
    done = shelllib.run(script, [pair], tmp_path=tmp_path)
    assert (done.returncode == 0) is ok


def test_an_env_pair_stays_a_closed_set_under_a_utf8_locale(tmp_path):
    """A bracket expression follows LC_CTYPE, and a root login shell on Ubuntu
    commonly has a UTF-8 one, under which a multibyte character counts as
    printable -- so a set that is "closed" only in the C locale is not closed.
    waku_env_pair_ok pins LC_ALL=C for the length of the test."""
    script = tmp_path / "call.sh"
    script.write_text(
        f'. "{CHECKS}"\nwaku_env_pair_ok "$1"\n', encoding="utf-8")
    done = shelllib.run(script, ["A=café"], tmp_path=tmp_path,
                        env={"LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"})
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


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("agent.waku.one", True),
        ("waku.example.com", True),
        ("a.io", True),
        ("x-y.z-w.io", True),
        # A single label cannot carry a wildcard certificate.
        ("localhost", False),
        # `*.*` used to be the whole of this check, and every one of these
        # satisfied it and then failed at Caddy or at ACME -- after the VM was
        # built, which is precisely what a preflight exists to prevent.
        (".", False),
        ("..", False),
        ("a..b", False),
        ("a b.c", False),
        ("*.waku.one", False),
        ("http://a.b", False),
        ("a.b:8443", False),
        # A trailing dot is a valid FQDN and not a valid site address here.
        ("agent.waku.one.", False),
        (".agent.waku.one", False),
        # A hyphen may not begin or end a label.
        ("-a.b", False),
        ("a-.b", False),
        # An IP address: the last label is all digits.
        ("1.2.3.4", False),
        ("a.4", False),
        # Upper case: gateway/config.py lowercases WAKU_APEX_HOST when it
        # reads it, so a mixed-case value would leave install.env saying one
        # thing and the running gateway using another.
        ("Agent.Waku.One", False),
        # A label over 63 characters.
        ("a" * 64 + ".com", False),
        ("a" * 63 + ".com", True),
    ])
def test_the_domain_is_a_closed_set_on_the_value(tmp_path, name, ok):
    script = tmp_path / "call.sh"
    script.write_text(f'. "{CHECKS}"\nwaku_is_hostname "$1"\n', encoding="utf-8")
    done = shelllib.run(script, [name], tmp_path=tmp_path)
    assert (done.returncode == 0) is ok


@pytest.mark.parametrize("name", ["localhost", "*.waku.one", "http://a.b",
                                  "1.2.3.4", "Agent.Waku.One", "a..b"])
def test_install_sh_refuses_a_domain_that_is_not_a_hostname(tmp_path, name):
    """A tenant host is <id>.<domain>, and <id> is a DNS label. An apex that
    cannot carry a wildcard certificate fails at ACME time -- after the VM is
    built -- unless it fails here."""
    done = shelllib.run(INSTALL, _required(tmp_path, domain=name),
                        tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert "must be a hostname with at least two labels" in done.stderr
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
        "sk-ant-abc\nWAKU_UPSTREAM_BASE_URL=http://evil.test\n",
        # A leading blank line does the same thing from the other end.
        "\nsk-ant-abc\n",
        # Whitespace either side is carried straight into the Authorization
        # header, where it fails as an authentication error rather than as a
        # configuration one.
        "  sk-ant-abc\n",
        "sk-ant-abc \n",
        "sk-ant abc\n",
        "sk-ant-abc\t\n",
        # A file saved on Windows. This is the likeliest real cause of the
        # refusal, which is why the message names a carriage return.
        "sk-ant-abc\r\n",
        "sk-ant-abc\r\n\r\n",
    ])
def test_a_platform_key_file_holding_anything_but_the_key_is_refused(tmp_path, text):
    """A CLOSED SET: printable, non-space characters only. Naming the ways a
    file can be wrong would miss the next one; naming what is allowed does
    not."""
    key = _key_file(tmp_path, text)
    done = shelllib.run(INSTALL, _required(tmp_path, key=key),
                        tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert "must hold the value and nothing else" in done.stderr
    assert "carriage return" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_a_real_anthropic_key_shape_is_accepted(tmp_path):
    """The other half of the closed set, and the half that matters most: a
    guard that refused the real thing would be found in production. This is
    the shape Anthropic issues -- base62 with `-` and `_`."""
    key = _key_file(tmp_path, "sk-ant-api03-AbC_dEf-123456789_XyZ-AA\n")
    done = shelllib.run(INSTALL, _required(tmp_path, key=key),
                        tmp_path=tmp_path, stubs=OUTSIDE)
    assert "must hold the value and nothing else" not in done.stderr


def test_a_platform_key_file_with_a_nul_byte_is_refused(tmp_path):
    """The one thing the character set CANNOT see, because command
    substitution drops NUL before the check runs: `sk-ant\\0abc` became
    `sk-antabc`, a silently mangled key that every later check accepted. The
    file is measured in bytes before it is read."""
    key = _key_file(tmp_path, b"sk-ant\x00abc\n")
    done = shelllib.run(INSTALL, _required(tmp_path, key=key),
                        tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert "holds a NUL byte" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_the_editors_trailing_newline_is_not_part_of_the_key(tmp_path):
    """Every editor ends a file with a newline, so a key file that has one is
    the common case and must be accepted.

    The assertion is POSITIVE: the run reaches the NEXT refusal, which is
    waku_require_root. An earlier version asserted only that the key messages
    were absent from stderr, and that version passed with the whole key-file
    validation block deleted. This one does not: delete the block and the run
    still reaches the root refusal, but delete the `$(cat ...)` read and the
    required-flag loop refuses `--platform-key-file` instead.
    """
    if os.geteuid() == 0:
        pytest.skip("as root the run goes past waku_require_root into /etc/os-release, "
                    "which this machine does not have")
    key = _key_file(tmp_path, "sk-ant-abc\n")
    done = shelllib.run(INSTALL, _required(tmp_path, key=key),
                        tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert "run this as root" in done.stderr, done.stderr
    assert shelllib.calls(tmp_path) == []


# --- --dns-env-file, the credential that can rewrite the zone ----------------
#
# The AWS secret access key given as `--dns-env AWS_SECRET_ACCESS_KEY=...` sat
# in root's .bash_history and in `ps` output for the whole install. It can
# rewrite the zone for this domain, so it can point the apex and every tenant
# host anywhere and mint a certificate for them -- a strictly larger blast
# radius than the model key's, and the same ruling applies.


def test_a_dns_env_file_that_is_not_there_is_refused(tmp_path):
    missing = tmp_path / "no-such-dns-env"
    done = shelllib.run(INSTALL, _required(tmp_path, dns_env_file=missing),
                        tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert f"--dns-env-file: no such file: {missing}" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_a_dns_env_file_is_required(tmp_path):
    """Not optional. A DNS-01 challenge with no credential produces a
    certificate that never issues, and the operator finds out from a browser
    rather than from the installer."""
    args = [a for a in _required(tmp_path) if a != "--dns-env-file"]
    args.remove(str(_dns_file(tmp_path)))
    done = shelllib.run(INSTALL, args, tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert "--dns-env-file is required" in done.stderr


@pytest.mark.parametrize(
    "text",
    ["", "\n\n\n", "# only a comment\n"])
def test_a_dns_env_file_with_no_pair_in_it_is_refused(tmp_path, text):
    path = _dns_file(tmp_path, text)
    done = shelllib.run(INSTALL, _required(tmp_path, dns_env_file=path),
                        tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert "holds no NAME=VALUE line" in done.stderr
    assert shelllib.calls(tmp_path) == []


@pytest.mark.parametrize(
    "text",
    [
        "AWS_ACCESS_KEY_ID\n",
        "AWS ACCESS KEY=x\n",
        "AWS_ACCESS_KEY_ID=\n",
        "AWS_ACCESS_KEY_ID=AKIA EXAMPLE\n",
        "AWS_ACCESS_KEY_ID=AKIAEXAMPLE\r\n",
        "1BAD=x\n",
    ])
def test_a_malformed_line_in_the_dns_env_file_is_refused(tmp_path, text):
    """Measured against a real `docker compose config`: a line with no `=` is
    accepted by Compose and leaves the variable UNSET, which looks exactly
    like a credential that was set."""
    path = _dns_file(tmp_path, text)
    done = shelllib.run(INSTALL, _required(tmp_path, dns_env_file=path),
                        tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert str(path) in done.stderr
    assert shelllib.calls(tmp_path) == []


# THE REFUSAL PATH IS WHERE A SECRET ESCAPES, and it is the path nobody tests
# because it is the one that is supposed to fail. `add_dns_env` used to end its
# message with `got: $1`, and $1 is the credential line -- so the first
# realistic operator mistake, a stray trailing space or a file saved on
# Windows, put the whole zone-rewriting AWS secret on stderr and into whatever
# scrollback, CI log or `tee` was capturing it.

_SECRET = "wJalrXUtnFEMIsecretK7MDENGbPxRfiCY"


@pytest.mark.parametrize(
    ("text", "line"),
    [
        # A trailing space: the likeliest mistake of all.
        (f"AWS_ACCESS_KEY_ID=AKIAEXAMPLE\nAWS_SECRET_ACCESS_KEY={_SECRET} \n", 2),
        # A file saved on Windows.
        (f"AWS_SECRET_ACCESS_KEY={_SECRET}\r\n", 1),
        # A missing '=' -- the whole line is then the secret and there is no
        # name to show, so the message must fall back to the position alone.
        (f"AWS_SECRET_ACCESS_KEY{_SECRET}\n", 1),
        # A blank first line and a comment, so the number is the editor's and
        # not the count of lines that carried a pair.
        (f"\n# route53\nAWS_SECRET_ACCESS_KEY={_SECRET}\t\n", 3),
    ])
def test_a_refusal_names_the_file_and_the_line_number_and_never_the_secret(
        tmp_path, text, line):
    path = _dns_file(tmp_path, text)
    done = shelllib.run(INSTALL, _required(tmp_path, dns_env_file=path),
                        tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert _SECRET not in done.stderr, "the credential reached the terminal"
    assert _SECRET not in done.stdout, "the credential reached the terminal"
    assert f"{path} line {line}" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_a_refusal_may_name_the_variable_because_a_name_is_not_a_secret(tmp_path):
    """The other half: refusing without saying anything useful sends the
    operator to read a file with `cat`, which is worse. A variable's NAME is
    safe to print and is shown when it can be recovered through the same
    closed set the value goes through."""
    path = _dns_file(tmp_path, f"AWS_SECRET_ACCESS_KEY={_SECRET} \n")
    done = shelllib.run(INSTALL, _required(tmp_path, dns_env_file=path),
                        tmp_path=tmp_path, stubs=OUTSIDE)
    assert "the value of AWS_SECRET_ACCESS_KEY is not usable" in done.stderr
    assert _SECRET not in done.stderr


def test_the_dns_env_flag_refuses_without_printing_its_value_either(tmp_path):
    """--dns-env is for non-secrets, but an operator who puts a credential
    there has already made one mistake and the refusal must not make a second
    one by copying it somewhere argv is not."""
    args = _required(tmp_path) + ["--dns-env", f"AWS_SECRET_ACCESS_KEY={_SECRET} "]
    done = shelllib.run(INSTALL, args, tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert _SECRET not in done.stderr
    assert "--dns-env:" in done.stderr


def test_a_dns_env_file_with_a_nul_byte_is_refused(tmp_path):
    """The same guard --platform-key-file has. Measured before the fix:
    `abc\\0def` was accepted and silently truncated to `abc` in caddy.env -- a
    credential that looks written and is wrong, failing at ACME hours later
    with no clue why. Two guards on the same class of input must not
    disagree."""
    path = _dns_file(tmp_path, b"AWS_SECRET_ACCESS_KEY=abc\x00def\n")
    done = shelllib.run(INSTALL, _required(tmp_path, dns_env_file=path),
                        tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert "holds a NUL byte" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_a_dns_env_files_last_line_is_read_even_without_a_newline(tmp_path):
    """A file written by `printf` with no trailing newline, or one a human
    truncated, is the common accident -- and losing its last line silently
    would lose a credential.

    The fixture holds ONE pair and no newline after it, so a `read` loop that
    dropped the last line would leave the file with nothing in it and the
    empty-file refusal fires. An earlier fixture had three lines, so dropping
    the last one still left two and this test passed with the bug in place.
    """
    path = _dns_file(tmp_path, "# route53\n\nAWS_ACCESS_KEY_ID=AKIAEXAMPLE")
    done = shelllib.run(INSTALL, _required(tmp_path, dns_env_file=path),
                        tmp_path=tmp_path, stubs=OUTSIDE)
    assert "holds no NAME=VALUE line" not in done.stderr, done.stderr
    assert "expected NAME=VALUE" not in done.stderr


def test_the_dns_env_flag_takes_the_same_closed_set_as_the_file(tmp_path):
    """--dns-env survives for a non-secret such as AWS_REGION, and it has to
    refuse exactly what the file refuses: an operator who moves a variable
    from one to the other must not find it accepted in one place and refused
    in the other."""
    args = _required(tmp_path) + ["--dns-env", "AWS_REGION"]
    done = shelllib.run(INSTALL, args, tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert "expected NAME=VALUE" in done.stderr
    assert "--dns-env:" in done.stderr
    assert shelllib.calls(tmp_path) == []


# --- the two guards that used to name characters instead of values -----------


@pytest.mark.parametrize("value", ["0", "00", "01", "-1", "1.5", "many"])
def test_max_running_is_a_closed_set_on_the_value(tmp_path, value):
    """`--max-running 0` used to pass a check whose own message said "positive
    integer". WAKU_MAX_RUNNING=0 is a VM on which no tenant can ever start,
    and waku_max_running's floor of 1 never applies to a value given by flag.

    An earlier version of this test accepted either the flag's message OR
    "run this as root", because the check still sat below waku_require_root.
    That made it pass for every value of every guard: it could not fail. The
    check moved up into the argument block -- where it belongs, because it is
    an argument check -- and the assertion is now on the message alone.
    """
    args = _required(tmp_path) + ["--max-running", value]
    done = shelllib.run(INSTALL, args, tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert "--max-running must be a whole number of at least 1" in done.stderr
    assert shelllib.calls(tmp_path) == []


@pytest.mark.parametrize("value", ["0", "0G", "010G", "1T", "1.5G", "1GB",
                                   "9999999999999G"])
def test_tenant_disk_is_a_closed_set_on_the_value(tmp_path, value):
    """The same, for the flag whose zero is the dangerous one: XFS reads
    bhard=0 as NO limit, so `--tenant-disk 0` put every tenant on an unbounded
    disk. Reachable here because the conversion moved into the argument
    block."""
    args = _required(tmp_path) + ["--tenant-disk", value]
    done = shelllib.run(INSTALL, args, tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert "--tenant-disk takes a whole number of at least 1" in done.stderr
    assert shelllib.calls(tmp_path) == []


def test_a_platform_key_file_stays_a_closed_set_under_a_utf8_locale(tmp_path):
    """A bracket expression follows LC_CTYPE. Under the UTF-8 locale a root
    login shell on Ubuntu commonly has, a multibyte character counts as
    printable and `[:graph:]` admits it -- so a set that is closed only in the
    C locale is not closed. read_secret_file pins LC_ALL=C for the test."""
    key = _key_file(tmp_path, "sk-ant-café\n")
    done = shelllib.run(INSTALL, _required(tmp_path, key=key),
                        tmp_path=tmp_path, stubs=OUTSIDE,
                        env={"LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"})
    assert done.returncode != 0
    assert "must hold the value and nothing else" in done.stderr


def test_max_running_given_as_an_empty_string_is_refused(tmp_path):
    """It used to fall through `[ -n "$max_running" ]` and be silently
    replaced by the value derived from memory, so an operator who fumbled a
    shell variable got a number they did not choose and no word about it.
    Empty is a value given, not a value withheld."""
    args = _required(tmp_path) + ["--max-running", ""]
    done = shelllib.run(INSTALL, args, tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert "--max-running must be a whole number of at least 1" in done.stderr


# --- the flags whose values are written into env files unvalidated ----------
#
# Every one of these becomes one NAME=VALUE line in config/. Whitespace there
# is carried into whatever reads it, and a newline writes a second setting.
# They are not credentials, so these refusals do print what was typed.


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--free-model", "claude haiku"),
        ("--free-model", "claude-haiku\nWAKU_FREE_MONTHLY_CAP_USD=1000"),
        ("--free-model", "claude-haiku-4-5 "),
        ("--supabase-publishable-key", "sb_publishable_x y"),
        ("--supabase-publishable-key", "sb_x\nWAKU_SUPABASE_AUDIENCE=evil"),
        ("--supabase-audience", "https://api.waku.one/mcp extra"),
        ("--data-device", "/dev/sdb1 "),
        ("--acme-email", "ops@example.test "),
    ])
def test_a_flag_whose_value_reaches_an_env_file_refuses_whitespace(
        tmp_path, flag, value):
    args = _required(tmp_path) + [flag, value]
    done = shelllib.run(INSTALL, args, tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert "printable characters with no space" in done.stderr
    assert shelllib.calls(tmp_path) == []


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("https://p.supabase.co", True),
        # The parser strips one trailing slash, so this is the same URL.
        ("https://p.supabase.co/", True),
        # The gateway appends /auth/v1 to it, so a path here builds a URL that
        # fetches nothing.
        ("https://p.supabase.co/auth", False),
        ("https://p.supabase.co:8443", False),
        # http would send an access token in clear.
        ("http://p.supabase.co", False),
        ("p.supabase.co", False),
        ("https://", False),
        ("https://localhost", False),
    ])
def test_the_supabase_url_is_a_closed_set(tmp_path, value, ok):
    args = _required(tmp_path) + ["--supabase-url", value]
    done = shelllib.run(INSTALL, args, tmp_path=tmp_path, stubs=OUTSIDE)
    refused = "--supabase-url must" in done.stderr
    assert refused is not ok, done.stderr


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("ops@example.test", True),
        ("ops+waku@example.test", True),
        ("ops@example", False),
        ("ops@@example.test", False),
        ("@example.test", False),
        ("ops@", False),
        ("ops.example.test", False),
    ])
def test_the_acme_email_is_a_closed_set(tmp_path, value, ok):
    """Let's Encrypt sends expiry warnings there, and a wrong one is only
    discovered when a certificate silently stops renewing."""
    args = _required(tmp_path) + ["--acme-email", value]
    done = shelllib.run(INSTALL, args, tmp_path=tmp_path, stubs=OUTSIDE)
    refused = "--acme-email" in done.stderr
    assert refused is not ok, done.stderr


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("route53", True),
        # The documented Cloudflare recipe: the module is the first word and
        # the rest is the argument Caddy's dns directive takes inline. The
        # whole string used to go to xcaddy as well, so this could not build.
        ("cloudflare {env.CLOUDFLARE_API_TOKEN}", True),
        ("digitalocean", True),
        ("Route53", False),
        ("caddy-dns/route53", False),
        ("-route53", False),
        ("route53-", False),
        ("", False),
    ])
def test_the_dns_provider_module_is_a_closed_set(tmp_path, value, ok):
    args = _required(tmp_path) + ["--dns-provider", value]
    done = shelllib.run(INSTALL, args, tmp_path=tmp_path, stubs=OUTSIDE)
    refused = "--dns-provider" in done.stderr
    assert refused is not ok, done.stderr


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("@v1.5.0", True),
        ("@latest", True),
        ("@v1.5.0-beta.1", True),
        # It is expanded inside the Dockerfile's RUN, where it is quoted -- and
        # a closed set here is the other half of that.
        ("v1.5.0", False),
        ("@v1.5.0 && curl evil.test", False),
        ("@", False),
        ("@$(id)", False),
        ("@v1;rm -rf /", False),
    ])
def test_the_dns_module_version_is_a_closed_set(tmp_path, value, ok):
    args = _required(tmp_path) + ["--dns-module-version", value]
    done = shelllib.run(INSTALL, args, tmp_path=tmp_path, stubs=OUTSIDE)
    refused = "--dns-module-version" in done.stderr
    assert refused is not ok, done.stderr


@pytest.mark.parametrize("flag", ["--dns-provider", "--platform-key-file", "--root"])
def test_a_flag_given_without_a_value_says_so(tmp_path, flag):
    """It used to die on bash's own `$2: unbound variable`. Still a refusal
    that ran nothing, but the one message in the file that did not read like
    the others."""
    done = shelllib.run(INSTALL, ["example.test", flag],
                        tmp_path=tmp_path, stubs=OUTSIDE)
    assert done.returncode != 0
    assert f"{flag} needs a value" in done.stderr
    assert shelllib.calls(tmp_path) == []
