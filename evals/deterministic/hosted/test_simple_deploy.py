"""Offline checks for the static deployment's authentication and isolation."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "simple_deploy", Path(__file__).resolve().parents[3] / "hosted/deploy/simple/render.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

HASH = "$2a$14$" + "a" * 53


def account(name="alice", host="alice.example.org", password_hash=HASH):
    return {"username": name, "hostname": host, "password_hash": password_hash}


def test_accounts_have_separate_data_and_no_shared_network_or_public_ports():
    compose, caddy = module.render([account(), account("bob", "bob.example.org")])
    alice = compose["services"]["waku-alice"]
    bob = compose["services"]["waku-bob"]
    assert alice["volumes"] == ["./tenants/alice/home:/data", "./tenants/alice/env:/work"]
    assert bob["volumes"] == ["./tenants/bob/home:/data", "./tenants/bob/env:/work"]
    assert set(alice["networks"]).isdisjoint(bob["networks"])
    assert "ports" not in alice and "ports" not in bob
    assert set(compose["services"]["caddy"]["networks"]) == {"account-alice", "account-bob"}
    sites = caddy.split("\n\n")
    assert len(sites) == 2
    assert "alice " + HASH in sites[0] and "bob " + HASH not in sites[0]
    assert "bob " + HASH in sites[1] and "alice " + HASH not in sites[1]
    for site in sites:
        assert "basic_auth {" in site
        assert "header_up -Authorization" in site
        assert "flush_interval -1" in site


@pytest.mark.parametrize("accounts", [
    [], [account("../alice")], [account(host="example.org {\nrespond hi")],
    [account(password_hash="plaintext")], [account(), account()],
    [account(), account("bob", "alice.example.org")],
])
def test_invalid_accounts_cannot_render_public_routes(accounts):
    with pytest.raises(ValueError):
        module.render(accounts)


def test_manual_certificate_mount_is_read_only_and_scoped_to_its_account():
    alice = account()
    alice["tls"] = "manual"
    compose, caddy = module.render([alice, account("bob", "bob.example.org")])
    assert "./tls:/certificates:ro" in compose["services"]["caddy"]["volumes"]
    assert all("certificates" not in volume for volume in
               compose["services"]["waku-alice"]["volumes"])
    sites = caddy.split("\n\n")
    assert "tls /certificates/alice/fullchain.pem /certificates/alice/privkey.pem" in sites[0]
    assert "    tls " not in sites[1]


def test_automatic_tls_does_not_mount_private_certificates():
    compose, caddy = module.render([account()])
    assert "./tls:/certificates:ro" not in compose["services"]["caddy"]["volumes"]
    assert "/certificates/" not in caddy


def test_unknown_tls_mode_is_rejected():
    alice = account()
    alice["tls"] = "../other"
    with pytest.raises(ValueError, match="TLS mode"):
        module.render([alice])
