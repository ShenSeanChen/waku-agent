"""DETERMINISTIC EVAL — the dashboard follows the Waku Memory design system.

design/tokens.css and design/controls.css are copies of Waku Memory's files.
These checks keep the copies unedited, and keep style.css and the inline
styles in js/ from writing values of their own. Read the "Design system"
section of waku/ops/static/README.md before changing any of them."""

from __future__ import annotations

import hashlib
import importlib.util
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "waku" / "ops" / "static"
DESIGN = STATIC / "design"
COPIED = ("tokens.css", "controls.css")


def _style() -> str:
    return (STATIC / "style.css").read_text()


def _index() -> str:
    return (STATIC / "index.html").read_text()


def _js() -> dict[str, str]:
    return {f.name: f.read_text() for f in sorted((STATIC / "js").glob("*.js"))}


def _blocks(css: str) -> list[tuple[str, str]]:
    """(selector, body) for every innermost rule, comments removed.

    Works through @media: the regex only matches a brace pair with no brace
    inside it, so the rules inside a media block are found one by one."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return [(m.group(1).strip(), m.group(2)) for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", css)]


def _decls(body: str) -> list[tuple[str, str]]:
    out = []
    for part in body.split(";"):
        if ":" in part:
            prop, val = part.split(":", 1)
            out.append((prop.strip().lower(), val.strip()))
    return out


def _declarations() -> list[tuple[str, str, str, str]]:
    """(file, selector, property, value) for style.css and every inline style="…" in js/."""
    rows = [("style.css", sel, p, v) for sel, body in _blocks(_style()) for p, v in _decls(body)]
    for name, src in _js().items():
        for m in re.finditer(r'style="([^"]*)"|style=\'([^\']*)\'', src):
            rows += [(name, "style=", p, v) for p, v in _decls(m.group(1) or m.group(2) or "")]
    return rows


def _is_svg_text(selector: str) -> bool:
    """The architecture and scatter charts are SVG. Their text sizes are in the
    viewBox's own units, drawn to fit boxes whose geometry is frozen, so the
    type scale does not apply to them."""
    return all(re.match(r"\.(arch|scatter)\b(?!-)", s.strip()) for s in selector.split(","))


def test_copied_files_match_source():
    """A hand edit to a copied file fails here. Change it in Waku Memory, then
    run scripts/sync_design.py."""
    source = (DESIGN / "SOURCE.md").read_text()
    recorded = {name: digest for digest, name in re.findall(r"^([0-9a-f]{64})  (\S+)$", source, re.MULTILINE)}
    assert set(recorded) == set(COPIED)
    for name in COPIED:
        assert hashlib.sha256((DESIGN / name).read_bytes()).hexdigest() == recorded[name], (
            f"design/{name} differs from the copy recorded in SOURCE.md")


def test_fonts_are_local():
    css = (DESIGN / "fonts.css").read_text()
    assert "http" not in css, "fonts.css must not fetch anything"
    urls = re.findall(r"url\(([^)]+)\)", css)
    assert len(urls) == 3
    for url in urls:
        assert (DESIGN / url.strip("'\"")).resolve().is_file(), f"fonts.css names a missing file: {url}"
    for face in ("InstrumentSans", "JetBrainsMono", "PlayfairDisplaySC"):
        assert (STATIC / "fonts" / f"OFL-{face}.txt").is_file(), f"no license for {face}"


def test_woff2_is_served_as_a_font():
    from waku.ops.dashboard import STATIC_TYPES

    assert STATIC_TYPES[".woff2"] == "font/woff2"


def test_sync_design_copies_and_records(tmp_path, monkeypatch):
    src = tmp_path / "memory"
    (src / "public" / "design").mkdir(parents=True)
    for name in COPIED:
        (src / "public" / "design" / name).write_text(f"/* {name} */\n")
    git = ["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "x"], check=True)

    spec = importlib.util.spec_from_file_location("sync_design", ROOT / "scripts" / "sync_design.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    dest = tmp_path / "design"
    dest.mkdir()
    monkeypatch.setattr(mod, "DEST", dest)

    assert mod.main(["sync_design.py", str(src)]) == 0
    source = (dest / "SOURCE.md").read_text()
    for name in COPIED:
        assert (dest / name).read_text() == f"/* {name} */\n"
        assert f"{hashlib.sha256((dest / name).read_bytes()).hexdigest()}  {name}" in source


# style.css keeps its short names in PR 1 so the inline styles in js/ keep
# working. Each one holds no value of its own; it points at a token.
ALIASES = {
    "--bg": "--surface-bg", "--panel": "--surface-paper",
    "--line": "--rule", "--line2": "--rule-hard",
    "--ink": "--text-ink", "--ink2": "--text-muted", "--ink3": "--text-faint",
    "--accent-soft": "--surface-raised", "--good-soft": "--surface-raised", "--bad-soft": "--surface-raised",
    "--good": "--ok", "--mono": "--face-mono",
}


def test_design_files_load_before_style():
    html = _index()
    order = ["design/fonts.css", "design/tokens.css", "design/type.css", "design/controls.css", "style.css"]
    pos = [html.find(f'href="/static/{name}"') for name in order]
    assert -1 not in pos, dict(zip(order, pos))
    assert pos == sorted(pos), "the design files must load before style.css"


def test_old_names_point_at_tokens():
    root = "".join(body for sel, body in _blocks(_style()) if sel == ":root").replace(" ", "").replace("\n", "")
    for old, token in ALIASES.items():
        assert f"{old}:var({token});" in root + ";", f"{old} should be var({token})"
    for token_name in ("--accent", "--bad"):
        assert f"{token_name}:" not in root, f"{token_name} is a token; style.css must not redefine it"
