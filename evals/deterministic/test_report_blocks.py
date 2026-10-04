"""DETERMINISTIC EVAL -- a research report's fenced blocks, drawn in the chat.

A report (`waku-report v1`, skills/research-report/SKILL.md) carries five
fenced blocks with JSON bodies: waku-metrics, waku-chart, waku-compare,
waku-timeline and waku-sources. The chat showed them as raw code: a
"WAKU-SOURCES" block of one long JSON line (Sean's rehearsal, 2026-10-04).
js/blocks.js draws them as UI, and these check it in node, against the same
renderMarkdown the chat calls:

  1. Each of the five renders as UI, with its raw JSON kept for Copy.
  2. A block whose JSON does not parse, or does not have the shape, or that
     is still streaming (no closing fence), is today's code block exactly.
  3. A source whose url is not http or https is text, not a link.
  4. HTML in any string a model wrote is escaped, never markup.

Bodies come from model output and web pages, so 3 and 4 are the security
half. CI has no browser; this skips where node is absent, like
test_embed_chat.py.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "waku" / "ops" / "static"
JS = STATIC / "js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")

# The chat's scripts, in their load order, with the DOM stubbed away: these
# renderers return HTML strings and touch nothing else.
_PROGRAM = r"""
const vm = require("vm"), fs = require("fs");
const ctx = {console, URL, JSON, Math, Number, String, Object, Array};
ctx.window = ctx;
ctx.document = {getElementById: () => null, querySelectorAll: () => [], documentElement: {dataset: {}}};
vm.createContext(ctx);
for (const f of FILES) vm.runInContext(fs.readFileSync(f, "utf8"), ctx, {filename: f});
const md = vm.runInContext("renderMarkdown", ctx);
console.log(JSON.stringify(INPUTS.map(t => md(t))));
"""


def _render(*texts: str) -> list[str]:
    files = [str(JS / n) for n in ("util.js", "ui.js", "blocks.js")]
    program = f"const FILES = {json.dumps(files)}; const INPUTS = {json.dumps(texts)};\n{_PROGRAM}"
    out = subprocess.run([NODE, "-e", program], capture_output=True, text=True,  # noqa: S603
                         timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _fence(lang: str, body: object | str) -> str:
    raw = body if isinstance(body, str) else json.dumps(body)
    return f"```{lang}\n{raw}\n```"


METRICS = [{"label": "Vendors", "value": "3"},
           {"label": "Most raised", "value": "$41M", "note": "Birchline, 2026-09"}]
CHART = {"type": "bar", "title": "Total funding raised", "unit": "$M",
         "series": [{"label": "Birchline", "value": 41}, {"label": "Kestrel", "value": 12},
                    {"label": "Tamsin", "value": 3.5}]}
COMPARE = {"columns": ["Free tier", "MCP server", "Self-host"],
           "rows": [{"name": "Birchline", "cells": [False, True, None]},
                    {"name": "Kestrel", "cells": [True, "Beta only"]}]}
TIMELINE = [{"date": "2026-09-12", "event": "Series A, $35M", "subject": "Birchline"},
            {"date": "2026-06", "event": "Launched an MCP server"}]
SOURCES = [{"title": "Birchline pricing", "url": "https://www.birchline.example/pricing",
            "via": "treg:treg.web.search", "cost_usd": 0.002},
           {"title": "Mem0, Zep, Letta, Supermemory official pages"}]


def _code_block(lang: str, raw: str) -> str:
    """Today's code block, as renderMarkdown draws any other fence."""
    esc = (raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
           .replace('"', "&quot;").replace("'", "&#39;"))
    return (f'<div class="mdcode"><div class="mdcode-head"><span class="mdcode-lang">{lang}</span>'
            f'<button type="button" class="btn btn-tertiary btn-sm mdcode-copy" onclick="copyCode(this)">'
            f'Copy</button></div><pre><code>{esc}</code></pre></div>')


# --- 1. each block renders ------------------------------------------------------


@needs_node
def test_each_block_renders_as_ui_and_keeps_its_json_for_copy():
    blocks = [("waku-metrics", METRICS), ("waku-chart", CHART), ("waku-compare", COMPARE),
              ("waku-timeline", TIMELINE), ("waku-sources", SOURCES)]
    html = _render(*(_fence(lang, body) for lang, body in blocks))
    for (lang, body), got in zip(blocks, html, strict=True):
        assert got.startswith(f'<div class="mdcode rblock rblock-{lang[5:]}">'), got[:120]
        assert "onclick=\"copyCode(this)\"" in got, f"{lang} lost its Copy button"
        hidden = re.search(r"<pre hidden><code>(.*)</code></pre>", got)
        assert hidden, f"{lang} has no raw JSON for Copy"
        assert json.loads(hidden.group(1).replace("&quot;", '"')) == body
    metrics, chart, compare, timeline, sources = html
    assert '<span class="rb-label">Most raised</span><span class="rb-value">$41M</span>' in metrics
    assert "Birchline, 2026-09" in metrics
    assert "Total funding raised" in chart and 'style="width:100%"' in chart
    assert 'style="width:29.3%"' in chart, "a bar is its share of the largest"
    assert ">$41M<" in chart and ">$3.5M<" in chart
    assert '<div class="rb-compare"><div class="tbl-wrap"><table class="tbl">' in compare
    assert compare.count("<tr>") == 3 and ">Birchline</td>" in compare
    for mark in ("rb-yes\">Yes", "rb-no\">No", "rb-unknown\">Unknown", ">Beta only<"):
        assert mark in compare, mark
    assert compare.count("rb-unknown") == 2, "a short row's missing answers are unknown"
    assert "12 Sep 2026" in timeline and "Jun 2026" in timeline
    assert '<span class="rb-subject">Birchline</span> Series A, $35M' in timeline
    assert sources.count("<li>") == 2
    assert ('<a href="https://www.birchline.example/pricing" target="_blank" '
            'rel="noopener noreferrer">Birchline pricing</a>') in sources
    assert "birchline.example · treg:treg.web.search · $0.002" in sources
    assert "<span>Mem0, Zep, Letta, Supermemory official pages</span>" in sources
    assert "Spent $0.002 on 1 paid call." in sources


@needs_node
def test_a_block_inside_a_report_still_renders_the_prose_around_it():
    text = ("Three companies sell hosted memory.\n" + _fence("waku-sources", SOURCES)
            + "\n## Gaps\nNone publish accuracy.")
    (html,) = _render(text)
    assert html.startswith('<div class="mdp">Three companies sell hosted memory.</div>'
                           '<div class="mdcode rblock rblock-sources">')
    assert html.endswith('<div class="mdh">Gaps</div><div class="mdp">None publish accuracy.</div>')


# --- 2. anything else falls back to today's code block ----------------------------


BROKEN = [
    ("waku-sources", '[{"title": "Mem0, Zep, Letta, Supermemory official p'),   # cut off
    ("waku-metrics", "not json"),
    ("waku-metrics", "[]"),
    ("waku-metrics", '[{"label": "Vendors"}]'),
    ("waku-chart", json.dumps({**CHART, "type": "pie"})),
    ("waku-chart", json.dumps({**CHART, "series": [{"label": "x", "value": "41"}]})),
    ("waku-compare", json.dumps({"columns": ["A"], "rows": [{"name": "x", "cells": [1, 2]}]})),
    ("waku-compare", json.dumps({"columns": ["A"], "rows": [{"name": "x", "cells": [{"b": 1}]}]})),
    ("waku-timeline", json.dumps([{"date": "last spring", "event": "x"}])),
    ("waku-sources", json.dumps([{"title": "x", "cost_usd": "free"}])),
    ("waku-sources", json.dumps({"title": "not a list"})),
    ("waku-map", json.dumps(METRICS)),                       # a block v1 does not know
]


@needs_node
@pytest.mark.parametrize(("lang", "raw"), BROKEN, ids=[f"{lang}-{n}" for n, (lang, _) in enumerate(BROKEN)])
def test_unreadable_or_unknown_blocks_are_todays_code_block(lang, raw):
    assert _render(_fence(lang, raw)) == [_code_block(lang, raw)]


@needs_node
def test_a_block_still_streaming_stays_code_until_its_fence_closes():
    """Mid-stream the closing fence has not arrived; even when the JSON so far
    parses, the block is code until the model finishes it."""
    raw = json.dumps(METRICS)
    (streaming,) = _render(f"```waku-metrics\n{raw}")
    assert streaming == _code_block("waku-metrics", raw)


# --- 3 and 4. untrusted bodies ---------------------------------------------------------


@needs_node
@pytest.mark.parametrize("url", [
    "javascript:alert(document.cookie)", " JavaScript:alert(1)", "data:text/html,<b>x</b>",
    "vbscript:x", "/relative/path", "//evil.example/x", "mailto:a@b.example",
])
def test_a_source_that_is_not_http_or_https_is_text_not_a_link(url):
    (html,) = _render(_fence("waku-sources", [{"title": "Pricing", "url": url}]))
    assert "rblock-sources" in html
    assert "<a " not in html and "href=" not in html
    assert "<span>Pricing</span>" in html


@needs_node
def test_html_a_model_wrote_is_escaped_in_every_block():
    evil = '<img src=x onerror=alert(1)>"\'<script>'
    bodies = [
        ("waku-metrics", [{"label": evil, "value": evil, "note": evil}]),
        ("waku-chart", {"type": "line", "title": evil, "unit": evil,
                        "series": [{"label": evil, "value": 1}]}),
        ("waku-compare", {"columns": [evil], "rows": [{"name": evil, "cells": [evil]}]}),
        ("waku-timeline", [{"date": "2026-10", "event": evil, "subject": evil}]),
        ("waku-sources", [{"title": evil, "url": 'https://a.example/"><script>x', "via": evil},
                          {"title": evil}]),
    ]
    html = _render(*(_fence(lang, body) for lang, body in bodies))
    for (lang, _), got in zip(bodies, html, strict=True):
        assert f"rblock-{lang[5:]}" in got, f"{lang} fell back instead of rendering"
        assert "<img" not in got and "<script" not in got, f"{lang}: {got}"
        assert "&lt;img src=x onerror=alert(1)&gt;" in got
    assert 'href="https://a.example/%22%3E%3Cscript%3Ex"' in html[4]


def test_both_pages_load_blocks_before_the_chat_renders():
    """renderMarkdown calls reportBlock, so blocks.js loads in both pages,
    after ui.js (it calls uiTable and uiButton) and before render.js."""
    for page in ("index.html", "embed.html"):
        scripts = re.findall(r'<script src="/static/js/([a-z]+\.js)"></script>',
                             (STATIC / page).read_text(encoding="utf-8"))
        assert scripts.index("ui.js") < scripts.index("blocks.js") < scripts.index("render.js"), page
