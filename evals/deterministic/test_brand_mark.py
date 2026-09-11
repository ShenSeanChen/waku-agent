"""The three copies of the Waku mark must stay one drawing.

The dashboard paints the mark through a CSS mask, so its colour comes from the
page and one file is enough. A README cannot do that — GitHub gives an <img>
no way to read the page theme — so the two inks ship as two more files, picked
by <picture>. Three files, one shape: that is a drift waiting to happen the
next time the bird is redrawn, and these tests are what catches it.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MASTER = ROOT / "waku" / "ops" / "static" / "waku-mark.svg"
VARIANTS = {
    "waku-mark-on-light.svg": "#161614",
    "waku-mark-on-dark.svg": "#C9CDD1",
}


def _geometry(svg: str) -> str:
    """Just the outline — the `d` attribute, with colour and layout stripped."""
    return re.search(r'\sd="([^"]+)"', svg).group(1)


def test_every_variant_draws_the_same_bird():
    master = _geometry(MASTER.read_text())
    for name in VARIANTS:
        variant = ROOT / "docs" / "brand" / name
        assert variant.is_file(), f"missing {variant}"
        assert _geometry(variant.read_text()) == master, (
            f"{name} has drifted from waku-mark.svg — regenerate it rather than "
            "editing it by hand"
        )


def test_each_variant_states_its_ink_outright():
    """A README SVG that inherits its colour renders as nothing on one theme."""
    for name, ink in VARIANTS.items():
        svg = (ROOT / "docs" / "brand" / name).read_text()
        assert f'fill="{ink}"' in svg, f"{name} should paint itself {ink}"
        assert "prefers-color-scheme" not in svg, (
            f"{name} must not rely on a media query — GitHub strips styling from "
            "README SVGs, and <picture> is what does the switching here"
        )


def test_the_readme_offers_both_inks():
    readme = (ROOT / "README.md").read_text()
    assert 'media="(prefers-color-scheme: dark)"' in readme
    for name in VARIANTS:
        assert f"docs/brand/{name}" in readme, f"README never references {name}"
