"""DETERMINISTIC EVAL — with no local model, the LLM page says how to get one.

Someone who opens the LLM page before installing anything used to meet one
sentence and a row of empty panes. Now the page is two steps, each one click:
a link to Ollama's own download page, and a button that copies the command
that downloads a model. The page still downloads and runs nothing itself:
the link opens in another tab and the button only fills the clipboard.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "waku" / "ops" / "static"
JS = (STATIC / "js" / "model.js").read_text()
CSS = (STATIC / "style.css").read_text()


def _empty_state() -> str:
    body = re.search(r"function mwNoModelHtml\(\)\{(.*?)\n\}", JS, re.DOTALL)
    assert body, "model.js needs mwNoModelHtml, the notice for a machine with no local model"
    return body.group(1)


def test_the_notice_links_to_ollamas_download_page_in_another_tab():
    link = re.search(r"<a [^>]*>", _empty_state())
    assert link and 'href="https://ollama.com/download"' in link.group(0)
    assert 'target="_blank"' in link.group(0) and "noopener" in link.group(0)


def test_one_button_copies_the_command_for_the_suggested_model():
    body = _empty_state()
    assert '"ollama pull " + MW_SUGGESTED' in body, "the command names the suggested model, in one place"
    assert 'onclick: "copyMsg(this)"' in body and 'data-text="${cmd}"' in body
    suggested = re.search(r'^const MW_SUGGESTED = "([a-z0-9.:-]+)";', JS, re.MULTILINE)
    assert suggested, "one constant names the model the page suggests"
    assert "m.name === MW_SUGGESTED" in JS, "the suggested model opens first when it is there"


def test_the_page_downloads_and_runs_nothing_itself():
    body = _empty_state()
    assert "fetch(" not in body and "mwPost" not in body


def test_with_no_model_the_empty_panes_are_hidden():
    assert 'classList.toggle("mw-empty", !mwModels.length)' in JS
    rule = re.search(r"\.mw\.mw-empty \.mw-top,\.mw\.mw-empty \.mw-pane\{([^}]*)\}", CSS)
    assert rule and "display:none" in rule.group(1)
