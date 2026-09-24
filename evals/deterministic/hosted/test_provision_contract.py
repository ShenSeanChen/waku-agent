"""Provisioning produces a waku that selects the free tier -- acceptance 6.

hosted/core/provision.py renders a file, and waku/config.py reads one. Neither
imports the other -- they must not -- so this is the only place the two meet.
It runs waku's own loader, waku's own model resolution and waku's own catalog
against the file provisioning wrote, with the four platform variables set in
os.environ around each call.

Those four are set here, by name, from the constants below. There is no
container image or compose template yet to read them from -- groups C and F
build that -- so this file is the only thing today that says which four a
tenant's waku needs and that they come from the environment rather than from
the .env provisioning writes. When C and F land, the names have to match, and
a mismatch shows up as a tenant whose dashboard reports the wrong model.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest
from dotenv import dotenv_values, find_dotenv

from hosted.core import provision, tenant

ROOT = Path(__file__).resolve().parents[3]
SOUL_TEMPLATE = ROOT / "hosted" / "templates" / "SOUL.md"

# A bridge address so the shape is real, and a port that is deliberately not
# one anything will use: D1 and F1 pick the proxy's real port, and a fixture
# that guessed it would read as a decision nobody made.
PLATFORM_BASE_URL = "http://10.88.0.1:8080"
PLATFORM_MODEL = "claude-sonnet-5"

# Lines the hosted persona has to carry. Pinned one by one rather than by
# comparing the rendered file with the template, because that comparison
# passes whatever the template says.
REQUIRED_SOUL_LINES = (
    "You are Waku, a personal assistant running in your user's own container.",
    "- Be honest about where things live. Your memory, calendar and skills live in",
)


@pytest.fixture
def provisioned(tmp_path):
    dirs = tenant.tenant_dirs(tmp_path, "abcdefghijkl")
    provision.provision(dirs, SOUL_TEMPLATE)
    return dirs


@pytest.fixture
def container(provisioned, monkeypatch):
    """What a tenant's waku starts into: a working directory holding the .env
    provisioning wrote, and the four platform variables in the environment.

    The directory is the fixture's own tmp_path, not /work. /work is where C2
    mounts it inside the container; nothing here cares about the name, because
    waku finds its .env with find_dotenv(usecwd=True) -- which is the thing the
    assertion below actually checks."""
    monkeypatch.chdir(provisioned.env)
    for name in ("WAKU_PROVIDER", "WAKU_MODEL", "WAKU_SMALL_MODEL",
                 "WAKU_API_KEY", "WAKU_BASE_URL", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WAKU_PLATFORM_BASE_URL", PLATFORM_BASE_URL)
    monkeypatch.setenv("WAKU_PLATFORM_TOKEN", "t" * 43)
    monkeypatch.setenv("WAKU_PLATFORM_MODEL", PLATFORM_MODEL)
    monkeypatch.setenv("WAKU_PLATFORM_SMALL_MODEL", PLATFORM_MODEL)
    # waku's own discovery, from the working directory it will really have.
    found = find_dotenv(usecwd=True)
    assert Path(found) == provisioned.env / ".env", (
        f"waku's find_dotenv(usecwd=True) found {found!r}, not the tenant's own .env")
    # Applied the way load_dotenv would, but through monkeypatch so it is
    # undone afterwards -- load_dotenv writes os.environ directly and would
    # leak WAKU_PROVIDER into every test that runs after this one.
    for key, value in dotenv_values(found).items():
        monkeypatch.setenv(key, value or "")
    return provisioned


def test_the_rendered_env_holds_only_the_provider_line(provisioned):
    """Everything else comes from the container environment, which outranks
    .env and which the tenant cannot edit. Writing the platform token here
    would hand the tenant's own dashboard a file that can redirect it."""
    text = (provisioned.env / ".env").read_text(encoding="utf-8")
    assert text == "WAKU_PROVIDER=waku-platform\n"
    assert "WAKU_PLATFORM_TOKEN" not in text


def test_the_env_file_is_private(provisioned):
    """Two assertions, and the second is the one that matters. The first
    compares the mode on disk with provision.ENV_MODE -- both sides move
    together, so on its own it is green for 0o600 and green for 0o644 alike.
    The second states the requirement itself, which is not a constant this
    repository is free to change: every tenant container runs as the same
    UID 10001, and a tenant's .env holds their own BYOK key the day the free
    tier stops being the only tier. Nothing outside the owner may read it."""
    mode = (provisioned.env / ".env").stat().st_mode & 0o777
    assert mode == provision.ENV_MODE
    assert mode & 0o077 == 0, (
        f".env is mode {mode:#o}: readable or writable by group or other. "
        "Every tenant container runs as UID 10001, so that is every tenant.")


def test_wakus_own_loader_selects_the_free_tier(container):
    from waku.config import Settings

    assert Settings().provider == "waku-platform"


def test_the_platform_model_is_both_defaults(container):
    """A provider switch writes WAKU_MODEL from the provider default, so the
    default has to be the deploy-time model or the proxy refuses it. The gate
    runs on the small model, and a small model outside the allowlist would be
    refused every turn and silently fail open."""
    from waku.loop.models import models_for

    assert models_for("waku-platform", "", "") == (PLATFORM_MODEL, PLATFORM_MODEL)


def test_models_are_listed_from_the_platform_base_url(container, monkeypatch):
    from waku.ops import catalog

    seen: dict[str, str] = {}

    def fake_urlopen(req, timeout=10):
        seen["url"] = req.full_url
        raise OSError("offline on purpose")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    # list_models caches its own failures in a module global for four minutes
    # (catalog.py:160). Clear it before and after so this test neither reads a
    # neighbour's entry nor leaves one for the rest of the session.
    catalog._models_cache.clear()
    try:
        catalog.list_models("waku-platform", use_cache=False)
    finally:
        catalog._models_cache.clear()
    assert seen["url"] == f"{PLATFORM_BASE_URL}/v1/models"


def test_the_soul_file_carries_the_hosted_lines(provisioned):
    text = (provisioned.home / "SOUL.md").read_text(encoding="utf-8")
    missing = [line for line in REQUIRED_SOUL_LINES if line not in text]
    assert not missing, f"hosted/templates/SOUL.md no longer says: {missing}"


def test_the_soul_file_is_the_template_unchanged(provisioned):
    assert ((provisioned.home / "SOUL.md").read_text(encoding="utf-8")
            == SOUL_TEMPLATE.read_text(encoding="utf-8"))


def test_provisioning_repairs_only_what_is_missing(provisioned):
    """It runs again before every start. A tenant who edited their SOUL.md
    keeps the edit; a tenant whose first provision died halfway is repaired."""
    (provisioned.home / "SOUL.md").write_text("mine now\n", encoding="utf-8")
    (provisioned.env / ".env").unlink()

    written = provision.provision(provisioned, SOUL_TEMPLATE)

    assert (provisioned.home / "SOUL.md").read_text(encoding="utf-8") == "mine now\n"
    assert (provisioned.env / ".env").read_text(encoding="utf-8") == "WAKU_PROVIDER=waku-platform\n"
    assert written == [provisioned.env / ".env"]
