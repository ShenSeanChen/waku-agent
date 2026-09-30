"""Tenant ids, project ids, fixed addresses and time zones.

Acceptance 4's second half ("an unknown zone is stored as UTC"). Everything
here is pure arithmetic and pattern matching; no filesystem, no network.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path

import pytest

from hosted.core import tenant


def test_an_id_is_twelve_characters_of_lowercase_base32():
    for _ in range(200):
        value = tenant.new_tenant_id()
        assert tenant.TENANT_ID_RE.match(value), value
        assert len(value) == 12
        assert set(value) <= set(tenant.ALPHABET)


def test_ids_are_not_derived_from_anything():
    """Two ids in a row must differ. A derived id would leak the email into
    a DNS label the whole internet can see."""
    assert len({tenant.new_tenant_id() for _ in range(500)}) == 500


@pytest.mark.parametrize("bad", [
    "", "short", "ABCDEFGHIJKL", "abcdefghijk1", "abcdefghijk_", "abcdefghijklm",
    "abcdefghijk8", "abcdefghijk9", "abcdefghijk0", None, 12, b"abcdefghijkl",
])
def test_anything_else_is_not_an_id(bad):
    """It is also a DNS label, which is why there is no underscore, no upper
    case and no 0, 1, 8 or 9 (design section 10 uses the same pattern)."""
    assert tenant.is_tenant_id(bad) is False


def test_a_token_is_urlsafe_and_hashes_to_hex():
    token = tenant.new_proxy_token()
    assert tenant.is_proxy_token(token)
    digest = tenant.token_hash(token)
    assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
    assert tenant.token_hash(token) == digest
    assert tenant.token_hash(tenant.new_proxy_token()) != digest


def test_the_two_directories_are_the_only_two():
    dirs = tenant.tenant_dirs(Path("/srv/waku/tenants"), "abcdefghijkl")
    assert dirs.home == Path("/srv/waku/tenants/abcdefghijkl/home")
    assert dirs.env == Path("/srv/waku/tenants/abcdefghijkl/env")


def test_a_bad_id_never_becomes_a_path():
    with pytest.raises(ValueError):
        tenant.tenant_dirs(Path("/srv/waku/tenants"), "../../etc")


def test_every_project_id_lands_between_the_gateway_and_the_dynamic_range():
    """The proxy binds the bridge's gateway address, and Docker hands out the
    last /24 dynamically. A fixed address inside either would be taken twice."""
    for pid in (tenant.FIRST_PROJECT_ID, 1000, 40000, tenant.LAST_PROJECT_ID):
        address = ipaddress.ip_address(tenant.address_for_project(pid))
        assert address in tenant.TENANT_SUBNET
        assert address > tenant.TENANT_GATEWAY
        assert address not in tenant.DYNAMIC_RANGE


def test_the_first_and_last_addresses_are_what_install_sh_will_be_told():
    assert tenant.address_for_project(2) == "10.88.0.2"
    assert tenant.address_for_project(tenant.LAST_PROJECT_ID) == "10.88.254.255"
    assert str(tenant.TENANT_GATEWAY) == "10.88.0.1"
    assert str(tenant.DYNAMIC_RANGE) == "10.88.255.0/24"


@pytest.mark.parametrize("bad", [0, 1, -3, tenant.LAST_PROJECT_ID + 1, True, "2", None, 2.0])
def test_a_project_id_outside_the_range_is_refused(bad):
    assert tenant.is_project_id(bad) is False
    with pytest.raises(ValueError):
        tenant.address_for_project(bad)


def test_project_ids_are_never_reused():
    """A moved or deleted tree keeps its XFS project id. Handing a freed id to
    a new tenant would attach them to the old tenant's quota accounting."""
    assert tenant.next_project_id([]) == tenant.FIRST_PROJECT_ID
    assert tenant.next_project_id([2, 3, 4]) == 5
    assert tenant.next_project_id([2, 9]) == 10        # not 3
    with pytest.raises(ValueError):
        tenant.next_project_id([tenant.LAST_PROJECT_ID])


def test_a_zone_python_knows_is_kept():
    assert tenant.normalise_timezone("Asia/Shanghai") == "Asia/Shanghai"
    assert tenant.normalise_timezone("UTC") == "UTC"


@pytest.mark.parametrize("bad", ["", "Mars/Olympus", "Asia/Shanghai/..", "../etc/passwd",
                                 None, 7, "UTC\x00"])
def test_a_zone_python_does_not_know_becomes_utc(bad):
    """The browser sends this, and so does /account. Storing it unchecked puts
    a tenant-controlled string into TZ in their container's environment.

    A LOWERCASE ZONE IS NOT IN THIS LIST, on purpose. zoneinfo resolves a zone
    by opening a file under the system tzdata directory, so "asia/shanghai"
    is unknown on a case-sensitive filesystem and known on a case-insensitive
    one -- it fails on Linux CI and passes on a maintainer's Mac, or the other
    way round depending on which side you assert. That is a property of the
    filesystem, not of this code, and a test that pins it is a test that goes
    red for nobody's mistake."""
    assert tenant.is_known_timezone(bad) is False
    assert tenant.normalise_timezone(bad) == "UTC"
