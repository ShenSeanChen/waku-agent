"""DETERMINISTIC EVAL — the rulebook stays short, linked and honest.

AGENTS.md is the first file every contributor and every coding agent reads.
These checks keep what it says true: its length cap, the links it routes
through, the docs index, the examples boundary and the no-emoji rule. A cap
with no check drifts, so every cap in docs/context/conventions.md §9 has a
test here."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
CONTEXT = DOCS / "context"
RULEBOOK = [
    ROOT / "AGENTS.md",
    ROOT / "CONTRIBUTING.md",
    DOCS / "README.md",
    DOCS / "status.md",
    *sorted(CONTEXT.glob("*.md")),
]

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
# Characters that render as colour emoji: the emoji blocks, anything forced to
# emoji presentation by U+FE0F, and the handful of older symbols that default
# to it (a coffee cup, a check box, a star). Typographic marks such as an
# arrow or a plain star stay allowed.
EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF]"
    "|[☀-➿⬀-⯿]️"
    "|[⌚⌛⏩-⏳☔☕⚡⚪⚫⚽⚾"
    "⛄⛅⛔⛪⛲⛳⛵⛺⛽✅✊✋"
    "✨❌❎❓-❕❗➕-➗➰➿⬛⬜"
    "⭐⭕]"
)


def _local_links(doc: Path) -> list[str]:
    return [
        t for t in LINK.findall(doc.read_text(encoding="utf-8"))
        if not t.startswith(("http://", "https://", "mailto:", "#"))
    ]


def test_agents_md_fits_its_cap():
    lines = (ROOT / "AGENTS.md").read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 100, (
        f"AGENTS.md is {len(lines)} lines; push detail down into docs/context/ "
        "instead of growing it")


def test_claude_md_only_imports_agents_md():
    """One rulebook for every agent: Claude Code reads CLAUDE.md, the others
    read AGENTS.md, and the import keeps the two from drifting apart."""
    assert (ROOT / "CLAUDE.md").read_text(encoding="utf-8").strip() == "@AGENTS.md"


def test_rulebook_links_resolve():
    broken = [
        f"{doc.relative_to(ROOT)} -> {target}"
        for doc in RULEBOOK
        for target in _local_links(doc)
        if not (doc.parent / target.split("#", 1)[0]).exists()
    ]
    assert not broken, "broken links:\n" + "\n".join(broken)


def test_every_doc_is_indexed():
    """A doc nobody links to is a doc nobody reads, so docs/README.md lists
    every Markdown file in docs/ and docs/context/."""
    index = DOCS / "README.md"
    linked = {(DOCS / t.split("#", 1)[0]).resolve() for t in _local_links(index)}
    docs = [p for p in (*DOCS.glob("*.md"), *CONTEXT.glob("*.md")) if p != index]
    missing = [str(p.relative_to(ROOT)) for p in docs if p.resolve() not in linked]
    assert not missing, f"add these to docs/README.md: {missing}"


def test_gotchas_are_dated_retirable_and_capped():
    entries = [line for line in (CONTEXT / "gotchas.md").read_text(encoding="utf-8").splitlines()
               if line.startswith("- **[")]
    assert entries, "gotchas.md has no entries in the expected shape"
    assert len(entries) <= 40, f"gotchas.md has {len(entries)} entries; retire some"
    bad = [e[:70] for e in entries
           if not re.match(r"- \*\*\[\d{4}-\d{2}-\d{2}\] ", e) or "_Retire when:" not in e]
    assert not bad, f"each gotcha needs a date and a Retire when: {bad}"


def test_status_is_short_and_dated():
    text = (DOCS / "status.md").read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 120, "status.md is rewritten whole, not appended to"
    assert re.search(r"\*\*Last updated:\*\* \d{4}-\d{2}-\d{2}", text)


def test_waku_never_imports_examples():
    offenders = []
    for py in (ROOT / "waku").rglob("*.py"):
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            if any(n == "examples" or n.startswith("examples.") for n in names):
                offenders.append(str(py.relative_to(ROOT)))
    assert not offenders, f"waku/ must not import examples/: {offenders}"


def test_no_emoji_in_rulebook_or_readme():
    hits = [
        f"{doc.relative_to(ROOT)}:{n}"
        for doc in (ROOT / "README.md", *RULEBOOK)
        for n, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1)
        if EMOJI.search(line)
    ]
    assert not hits, f"no emojis in docs prose: {hits}"


def test_ci_table_names_real_checks():
    """Every script or test that AGENTS.md's "What CI blocks" table names
    exists, so the table cannot promise a check that was deleted."""
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    section = text.split("## What CI blocks", 1)[1].split("\n## ", 1)[0]
    paths = re.findall(r"`((?:evals|scripts|\.github)/[^`\s]+)`", section)
    assert paths, "the CI table names no checks"
    missing = [p for p in paths if not (ROOT / p).exists()]
    assert not missing, f"the CI table names checks that do not exist: {missing}"
