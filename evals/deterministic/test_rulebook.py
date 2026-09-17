"""DETERMINISTIC EVAL — the rulebook stays short, linked and honest.

AGENTS.md is the first file every contributor and every coding agent reads.
These checks keep what it says true: its length cap, the links it routes
through, the docs index, the examples boundary and the no-emoji rule. A cap
with no check drifts, so every cap in docs/context/conventions.md §9 has a
test here."""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
CONTEXT = DOCS / "context"
LAB = ROOT / "lab"
LAB_TOPICS = sorted(p for p in LAB.iterdir() if p.is_dir() and not p.name.startswith(("_", ".")))
PLAYBOOK = ("The question", "What we connect", "Run it", "What we found", "Video angle", "Graduation")
README_CAP = 200
RULEBOOK = [
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "CONTRIBUTING.md",
    DOCS / "README.md",
    DOCS / "status.md",
    *(DOCS / name for name in ("getting-started.md", "tour.md", "evals.md", "commands.md", "roadmap.md")),
    *sorted(CONTEXT.glob("*.md")),
    ROOT / "examples" / "README.md",
    LAB / "README.md",
    *(topic / "README.md" for topic in LAB_TOPICS),
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


def test_readme_stays_a_landing_page():
    """The README answers what this is, why it matters and how to start. It
    grew to 493 lines once; the long material lives in docs/ now."""
    lines = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    assert len(lines) <= README_CAP, (
        f"README.md is {len(lines)} lines (cap {README_CAP}); move the detail into docs/ "
        "and link it from the Docs table")


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


def _reaches_outside(node: ast.AST) -> bool:
    """True for an import of examples/lab, or a path literal that points there."""
    if isinstance(node, ast.Import):
        names = [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom) and node.module:
        names = [node.module]
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        names = [node.value.replace("/", ".")]
    else:
        return False
    return any(n in ("examples", "lab") or n.startswith(("examples.", "lab.")) for n in names)


def test_product_and_evals_never_reach_into_examples_or_lab():
    """The dependency runs one way: product code and the gate never import,
    run or read anything in examples/ or lab/ (conventions §6, rules 1 and 3)."""
    this = Path(__file__).resolve()
    offenders = sorted({
        str(py.relative_to(ROOT))
        for top in ("waku", "evals")
        for py in (ROOT / top).rglob("*.py")
        if py.resolve() != this
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8")))
        if _reaches_outside(node)
    })
    assert not offenders, f"these reach into examples/ or lab/: {offenders}"


def test_lab_topics_follow_the_playbook():
    assert LAB_TOPICS, "lab/ has no topics"
    problems = []
    for topic in LAB_TOPICS:
        readme = topic / "README.md"
        if not readme.exists():
            problems.append(f"{topic.name}: no README.md")
            continue
        text = readme.read_text(encoding="utf-8")
        headings = {line[3:].strip() for line in text.splitlines() if line.startswith("## ")}
        missing = [h for h in PLAYBOOK if not any(x.startswith(h) for x in headings)]
        if missing:
            problems.append(f"{topic.name}: missing {missing}")
        if not re.search(r"Verified against: .+, \d{4}-\d{2}-\d{2}", text):
            problems.append(f"{topic.name}: no 'Verified against: <tool> <version>, <date>' line")
    assert not problems, "copy lab/_template/README.md:\n" + "\n".join(problems)


def test_lab_never_ships():
    build = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["hatch"]["build"]
    assert build["targets"]["wheel"]["packages"] == ["waku"]
    assert "lab" in build["targets"]["sdist"]["exclude"]


def test_no_retired_waku_memory_address():
    """Waku Memory's MCP server is https://api.waku.one/mcp. The address before
    it now refuses clients, so a doc that still shows it hands readers a setup
    that cannot work."""
    docs = [ROOT / "README.md", *DOCS.rglob("*.md"), *(ROOT / "examples").rglob("*"), *LAB.rglob("*.md")]
    stale = [str(p.relative_to(ROOT)) for p in docs
             if p.is_file() and "cloudfront.net" in p.read_text(encoding="utf-8", errors="ignore")]
    assert not stale, f"use https://api.waku.one/mcp in: {stale}"


def test_no_emoji_in_rulebook_or_readme():
    hits = [
        f"{doc.relative_to(ROOT)}:{n}"
        for doc in RULEBOOK
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


# Topics whose board sources reached main before sources went private.
BOARD_SOURCES_PREDATE_RULE = {"kimi-k3", "pi-agent"}


def test_lab_topics_commit_screenshots_not_board_sources():
    """A board drawn for a video stays private: its .excalidraw source and the
    script that draws it live outside the repo, and the topic commits PNG
    screenshots (conventions §6)."""
    leaked = [str(p.relative_to(ROOT)) for topic in LAB_TOPICS
              if topic.name not in BOARD_SOURCES_PREDATE_RULE
              for p in topic.rglob("*.excalidraw")]
    assert not leaked, f"keep board sources out of the repo; commit a PNG in screenshots/ instead: {leaked}"
