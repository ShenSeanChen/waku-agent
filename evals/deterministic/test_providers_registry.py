"""waku/providers.toml — the provider list, as data.

A provider is one table in that file plus a logo. These are the checks that
used to be spread across four hand-edited eval files: that a row is complete,
that its key is reachable by a human, that the dashboard has a picture for it,
and that the loop can actually build a Provider from it.

Everything here reads the file. Adding a provider adds cases automatically; no
test needs editing, which is the whole point of moving the list out of Python.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from waku.loop.models import PROVIDERS, REGISTRY, Provider

REGISTRY_PATH = Path(__file__).resolve().parents[2] / "waku" / "providers.toml"
LOGOS = Path(__file__).resolve().parents[2] / "waku" / "ops" / "static" / "logos"
NAMES = sorted(REGISTRY)
REQUIRED = ("kind", "key_env", "model", "small_model", "key_url", "price")


def test_the_file_is_the_registry():
    """models.py must not grow a second, hand-written list beside it."""
    with REGISTRY_PATH.open("rb") as fh:
        on_disk = tomllib.load(fh)
    assert set(on_disk) == set(PROVIDERS), "providers.toml and PROVIDERS disagree"


@pytest.mark.parametrize("name", NAMES)
def test_every_row_is_complete(name):
    row = REGISTRY[name]
    missing = [field for field in REQUIRED if not row.get(field)]
    assert not missing, f"{name}: missing {', '.join(missing)}"


@pytest.mark.parametrize("name", NAMES)
def test_the_wire_format_is_one_of_the_two_that_exist(name):
    """The loop speaks Anthropic's Messages shape; the openai adapter
    translates. A third value would silently build a client that cannot talk."""
    assert REGISTRY[name]["kind"] in ("anthropic", "openai"), \
        f"{name}: kind {REGISTRY[name]['kind']!r} is not a wire format Waku has"


@pytest.mark.parametrize("name", NAMES)
def test_the_key_url_is_a_page_a_human_can_open(name):
    """It is printed when the key is missing. Pointing at .env.example was
    useless advice for anyone who installed from PyPI -- that file is not there."""
    url = REGISTRY[name]["key_url"]
    assert url.startswith("https://"), f"{name}: key_url {url!r} is not a URL"


@pytest.mark.parametrize("name", NAMES)
def test_price_is_a_pair_of_positive_numbers(name):
    price = REGISTRY[name]["price"]
    assert len(price) == 2, f"{name}: price must be [in, out]"
    assert all(isinstance(v, int | float) and v >= 0 for v in price), \
        f"{name}: price {price} is not two positive numbers"


@pytest.mark.parametrize("name", NAMES)
def test_the_dashboard_has_a_logo_for_it(name):
    """The Models grid renders /static/logos/<name>.svg. A missing file is an
    invisible broken image, so it is cheaper to fail here."""
    assert (LOGOS / f"{name}.svg").exists(), f"{name}: no waku/ops/static/logos/{name}.svg"


@pytest.mark.parametrize("name", NAMES)
def test_the_row_builds_a_provider(name):
    built = PROVIDERS[name]
    assert isinstance(built, Provider)
    assert built.default_pair(), f"{name}: no flagship/fast pair to pin"


@pytest.mark.parametrize("name", NAMES)
def test_a_regional_endpoint_needs_its_own_env_var(name):
    """A provider with separately-issued regional keys keeps its endpoint in a
    provider-scoped variable. Reusing the global WAKU_BASE_URL would leak one
    provider's endpoint into another."""
    row = REGISTRY[name]
    if row.get("endpoints"):
        assert row.get("base_url_env"), f"{name}: has endpoints but no base_url_env"
        for endpoint in row["endpoints"]:
            assert endpoint["label"] and endpoint["base_url"].startswith("https://")


def test_no_two_providers_share_a_key_variable():
    """Two rows on one variable make "is this configured" unanswerable."""
    seen: dict[str, str] = {}
    for name, row in REGISTRY.items():
        key = row["key_env"]
        # opencode's two endpoints are separate products with separate keys;
        # if that ever changes, this is the test that should be argued with.
        assert key not in seen, f"{name} and {seen[key]} both use {key}"
        seen[key] = name
