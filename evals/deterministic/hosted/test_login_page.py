"""The sign-in page: its assets, its one Supabase call, and the design system.

WHY THIS FILE EXISTS. On 2026-09-27 the page stopped loading a 218 KB
Supabase bundle to make one request, and started using the Waku design
system. Both changes moved something out of a library's hands and into this
repo's, and each created a way to be silently wrong that nothing else checks:

  the copies drift      design/ and fonts/ here are copies of waku/ops/static/,
                        because the services image's build context refuses
                        waku/ outright. A copy nobody compares is a fork.
  an asset is orphaned  the page references a file by URL; the gateway serves
                        an ALLOWLIST. Add a <link> and forget the entry and
                        the page loads unstyled, with a 404 nobody reads.
  the request drifts    login.js now writes the /auth/v1/otp call by hand. The
                        shape is what 2.117.1 sent; nothing but this file says
                        so, and the failure is invisible until a real sign-in.

The third is the reason the first two are written as they are. Each derives
its fixture list FROM the page rather than from a list kept beside it: a
hand-kept list of assets is a list of the assets somebody remembered.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hosted.gateway.app import STATIC_FILES

ROOT = Path(__file__).resolve().parents[3]
HOSTED_STATIC = ROOT / "hosted" / "gateway" / "static"
WAKU_STATIC = ROOT / "waku" / "ops" / "static"
LOGIN_HTML = ROOT / "hosted" / "templates" / "login.html"
LOGIN_CSS = HOSTED_STATIC / "login.css"
LOGIN_JS = HOSTED_STATIC / "login.js"

# Every copied file, as the pair (path under hosted/gateway/static, path under
# waku/ops/static). Derived by walking the copies, so a file added to design/
# or fonts/ without an original here is a failure rather than an omission.
# design/SOURCE.md is NOT a copy: it is this tree's own note saying why the
# rest of the directory is one. It is the only exception, and naming it here
# rather than filtering by extension keeps a second .md from sneaking in.
NOT_A_COPY = {"design/SOURCE.md"}

COPIES = sorted(
    (p.relative_to(HOSTED_STATIC) for p in HOSTED_STATIC.rglob("*")
     if p.is_file()
     and p.relative_to(HOSTED_STATIC).parts[0] in ("design", "fonts")
     and p.relative_to(HOSTED_STATIC).as_posix() not in NOT_A_COPY),
    key=str,
)


def test_there_are_copies_to_check():
    """The two tests below iterate COPIES. An empty list passes both of them
    while proving nothing -- the failure mode this project has now found
    sixteen times. This is the fixture list asserting it is not empty."""
    assert len(COPIES) >= 8, COPIES


@pytest.mark.parametrize("relative", COPIES, ids=str)
def test_every_copied_design_file_matches_wakus(relative):
    """Byte for byte. SOURCE.md says these are copies; this is what makes
    that sentence true rather than a hope. The mark is checked too, by the
    test below, because it does not live under design/."""
    mine = (HOSTED_STATIC / relative).read_bytes()
    theirs = (WAKU_STATIC / relative).read_bytes()
    assert mine == theirs, (
        f"{relative} has drifted from waku/ops/static/{relative}. The master "
        "is Waku Memory; sync there, then copy both.")


def test_the_mark_matches_wakus():
    assert (HOSTED_STATIC / "waku-mark.svg").read_bytes() == (
        (WAKU_STATIC / "waku-mark.svg").read_bytes())


def _referenced(text: str) -> set[str]:
    """Every /auth/static/ URL a file names, as the allowlist key it needs."""
    return set(re.findall(r"/auth/static/([A-Za-z0-9._/-]+)", text))


def test_every_asset_the_page_references_is_in_the_allowlist():
    """THE SECOND CONSUMER. STATIC_FILES had one reader, `_static`, and its
    test asked whether the names in it are served. The page is the other
    reader, and it asks the opposite question: is everything I name here in
    that dict? A <link> without an entry is a 404 and an unstyled page.
    """
    named = _referenced(LOGIN_HTML.read_text(encoding="utf-8"))
    assert named, "the page references no static asset at all"
    missing = sorted(name for name in named if name not in STATIC_FILES)
    assert not missing, f"the page names files the gateway will not serve: {missing}"


def test_every_font_the_stylesheet_names_is_served_and_exists():
    """fonts.css is a THIRD consumer, and the sneakiest: its url()s are
    relative, resolve one directory up, and are requested by the browser only
    after the CSS parses. A missing face is a silent fallback to system-ui --
    the exact look this page was changed to stop having."""
    css = (HOSTED_STATIC / "design" / "fonts.css").read_text(encoding="utf-8")
    urls = re.findall(r"url\(([^)]+)\)", css)
    assert urls, "fonts.css names no font file"
    for raw in urls:
        # Resolved the way the browser resolves it: this stylesheet is
        # served from /auth/static/design/, so `../fonts/x` is
        # /auth/static/fonts/x, which is the allowlist key `fonts/x`.
        target = raw.strip("'\" ")
        key = target[3:] if target.startswith("../") else f"design/{target}"
        assert key in STATIC_FILES, f"fonts.css names {raw}, which is not served"
        assert (HOSTED_STATIC / key).is_file(), f"fonts.css names a missing file: {raw}"


def test_everything_served_is_a_file_that_exists():
    """The allowlist's own values. A typo in a key is caught by the tests
    above; a typo in a VALUE is a 500 on a path that looks allowed."""
    for key, (filename, _type) in STATIC_FILES.items():
        assert (HOSTED_STATIC / filename).is_file(), f"{key} maps to a missing file"


def test_fonts_are_not_served_as_text():
    """A woff2 with `charset=utf-8` claims to be text in an encoding. It is
    bytes. The content type is also what stops a strict client refusing the
    face outright."""
    for key, (_filename, content_type) in STATIC_FILES.items():
        if key.endswith(".woff2"):
            assert content_type == "font/woff2", key


# --- the design system ---------------------------------------------------


def test_the_login_stylesheet_writes_no_colour_literal():
    """The dashboard's own rule (test_design_system.test_no_colour_literals),
    applied to this page for the same reason: a literal here is a second
    source of truth for a value tokens.css already owns, and it is the one
    that does not change when the system does."""
    css = LOGIN_CSS.read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    literals = re.findall(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(", css)
    assert not literals, f"login.css writes colours instead of tokens: {literals}"


def test_the_login_stylesheet_takes_its_sizes_from_the_scale():
    """Font sizes come from --text-*. A px here is a size the type scale does
    not know about, which is how a page stops matching the product while
    every individual rule still looks reasonable."""
    css = re.sub(r"/\*.*?\*/", "", LOGIN_CSS.read_text(encoding="utf-8"), flags=re.DOTALL)
    sizes = re.findall(r"font-size:\s*([^;]+);", css)
    assert sizes, "login.css sets no font size"
    off = [value for value in sizes if "var(--text-" not in value]
    assert not off, f"font sizes bypass the scale: {off}"


def test_the_field_and_the_button_carry_a_data_slot():
    """controls.css reaches a control through [data-slot] and nothing else:
    the focus ring, the disabled colours and the one transition are all keyed
    on it. Without the attribute the page looks styled and a keyboard user
    has no focus ring, which is the failure that file was written about."""
    html = LOGIN_HTML.read_text(encoding="utf-8")
    assert 'data-slot="input"' in html
    assert 'data-slot="button"' in html


def test_the_design_system_loads_before_the_page_that_reads_it():
    """A stylesheet cannot read a custom property declared in a file that
    loads after it. Order in the document is the whole contract."""
    html = LOGIN_HTML.read_text(encoding="utf-8")
    order = [name for name in re.findall(r'href="/auth/static/([^"]+)"', html)]
    assert "login.css" in order
    for token_file in ("design/tokens.css", "design/type.css", "design/fonts.css"):
        assert order.index(token_file) < order.index("login.css"), token_file


# --- the one Supabase call ------------------------------------------------


def test_the_otp_request_is_the_one_the_library_sent():
    """@supabase/supabase-js 2.117.1's signInWithOtp, written out.

    Taken from the bundle this page used to load, before it was deleted:

        POST {url}/auth/v1/otp?redirect_to={encoded}
        apikey, Authorization: Bearer, Content-Type: application/json
        {email, data, create_user, gotrue_meta_security,
         code_challenge, code_challenge_method}

    Every one of these is load-bearing and none of them is visible in a unit
    test of our own code: drop `apikey` and Supabase answers 401; drop
    `redirect_to` and the magic link sends people to the project's default
    URL, which is not this deployment.
    """
    js = LOGIN_JS.read_text(encoding="utf-8")
    assert "/auth/v1/otp?redirect_to=" in js
    assert "encodeURIComponent(location.origin" in js
    for header in ('"apikey"', '"Authorization": "Bearer "', '"Content-Type": "application/json"'):
        assert header in js, header
    for field in ("email:", "data:", "create_user:", "gotrue_meta_security:",
                  "code_challenge:", "code_challenge_method:"):
        assert field in js, field


def test_the_page_loads_no_vendored_library():
    """The bundle is gone and does not come back by another name. 32 KiB is
    well above anything this page needs (its largest asset is a 40 KB font)
    and well below the 218 KB that made this a latency bug."""
    html = LOGIN_HTML.read_text(encoding="utf-8")
    scripts = re.findall(r'<script[^>]*src="([^"]+)"', html)
    assert scripts == ["/auth/static/login.js"], scripts
    # The two data-supabase-* attributes are the page's whole Supabase
    # surface now, so the check is on what it LOADS, not on the word.
    assert not [src for src in scripts if "supabase" in src.lower()]
    assert not [name for name in STATIC_FILES if "supabase" in name.lower()]
    for key, (filename, content_type) in STATIC_FILES.items():
        if content_type == "text/javascript":
            size = (HOSTED_STATIC / filename).stat().st_size
            assert size < 32 * 1024, f"{key} is {size} bytes; is a library back?"
