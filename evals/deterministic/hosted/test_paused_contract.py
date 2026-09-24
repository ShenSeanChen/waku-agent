"""The gateway's paused reply and the page that reads it -- acceptance 9.

The gateway sends a JSON body; main.js tests one field of it. Two string
literals in two languages in two directories, and nothing but this file holds
them together. A3 shipped the reader; B2 shipped the writer.

BOTH SIDES ARE ENUMERATED FROM SOURCE. An earlier draft enumerated the page
and hand-wrote the gateway as `{policy.PAUSED_CODE}`, which meant a second
code added in group D or E -- a `maintenance` or an `at_capacity` body, both
of which the spec already defines as states -- would sit there with no page
branch reading it and the test would stay green. The gateway side is now
`policy.CODES`, which is the set every hosted error body is built from.

AND THE PAGE SIDE IS CLOSED AGAINST SPELLING. It does not look for a
comparison shape; it counts every occurrence of the `.code` token in the
dashboard's JavaScript and requires the comparison scanner to have accounted
for all of them. A `switch (body.code)`, a `body.code == "paused"` with two
equals signs, or an `includes(body.code)` leaves a token the scanner did not
claim, and the test fails asking for the scanner to be extended rather than
passing while a branch goes unread. That is the A5 inversion: the closed set
is the token, and the recognised forms are checked against it.
"""

from __future__ import annotations

import re
from pathlib import Path

from hosted.core import policy

ROOT = Path(__file__).resolve().parents[3]
JS_DIR = ROOT / "waku" / "ops" / "static" / "js"
MAIN_JS = JS_DIR / "main.js"

# Every `.code` occurrence, whatever it is doing. This is the closed set.
CODE_TOKEN = re.compile(r"\.code\b")
# The forms this file can read a code out of: `x.code === "s"`, `x.code == "s"`
# and either with the sides swapped.
CODE_COMPARISON = re.compile(
    r'\.code\s*===?\s*"([^"]*)"'
    r'|"([^"]*)"\s*===?\s*[\w.$\[\]]*\.code')

# Codes local waku reads for its own reasons, unrelated to hosting. Empty
# today, and it stays a list somebody has to add to with a reason beside it --
# not a scan that quietly excuses whatever it finds.
LOCAL_ONLY_CODES: set[str] = set()

# JavaScript's two comment forms. The fallback-sentence test below reads code
# only, because main.js QUOTES the sentence in a comment as well as writing it
# in the fallback -- a plain `sentence in main.js` was green with the fallback
# changed to "Paused. Wake it up." and only the comment still carrying the
# gateway's wording. Proved by running it, which is why these two lines exist.
# Over-stripping (a "//" inside a string literal) can only delete code and
# fail this loudly; JavaScript has no third comment form to under-strip.
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"(?m)//.*$")


def _code_only(text: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))


def _js_files() -> list[Path]:
    return sorted(JS_DIR.rglob("*.js"))


def test_the_javascript_this_contract_reads_is_actually_there():
    """Every assertion below is a claim about a set of files. An empty set
    satisfies all of them, so a moved directory or a renamed main.js would
    turn this whole contract green and silent."""
    assert MAIN_JS.is_file(), f"{MAIN_JS} is gone: this contract now guards nothing"
    assert len(_js_files()) > 1, f"only {len(_js_files())} JavaScript files under {JS_DIR}"
    tokens = sum(len(CODE_TOKEN.findall(_code_only(p.read_text(encoding="utf-8"))))
                 for p in _js_files())
    assert tokens, (
        "no `.code` token anywhere in the dashboard's JavaScript. The page no "
        "longer reads the gateway's code field, so either A3's reader was "
        "removed or it was rewritten in a shape this file cannot see.")


def test_every_code_token_in_the_dashboard_is_one_this_file_can_read():
    """The guard below reads a set of comparison shapes. This is the check
    that the shapes cover every `.code` there is."""
    unread = {}
    for path in _js_files():
        text = _code_only(path.read_text(encoding="utf-8"))
        tokens = len(CODE_TOKEN.findall(text))
        claimed = len(CODE_COMPARISON.findall(text))
        if tokens != claimed:
            unread[path.name] = f"{tokens} `.code` tokens, {claimed} read as comparisons"
    assert not unread, (
        f"`.code` is used in a shape this contract cannot read: {unread}\n"
        "Extend CODE_COMPARISON, or write the check as a plain "
        '`x.code === "literal"`. Leaving it unread means a page branch the '
        "gateway may never trigger, or a gateway code no page reads.")


def _codes_the_page_reads() -> set[str]:
    found: set[str] = set()
    for path in _js_files():
        for left, right in CODE_COMPARISON.findall(
                _code_only(path.read_text(encoding="utf-8"))):
            found.add(left or right)
    return found - LOCAL_ONLY_CODES


def test_the_page_reads_exactly_the_codes_the_gateway_sends():
    page, gateway = _codes_the_page_reads(), set(policy.CODES)
    assert page == gateway, (
        f"codes the gateway can send that no page branch reads: "
        f"{sorted(gateway - page)}\n"
        f"codes the page reads that the gateway never sends: "
        f"{sorted(page - gateway)}\n"
        "Add a code to hosted/core/policy.py's CODES and to the dashboard's "
        "JavaScript in the same PR, or do neither. A code local waku reads "
        "for its own reasons goes in LOCAL_ONLY_CODES with a line saying why.")


def test_every_declared_code_is_actually_used_by_a_body():
    """CODES is the declaration; PAUSED_BODY is the one body so far. A code
    declared and never put in a body would satisfy the test above while
    nothing sends it."""
    bodies = [policy.PAUSED_BODY]
    assert {body["code"] for body in bodies} == set(policy.CODES)


def test_the_body_is_the_shape_the_spec_names():
    assert policy.PAUSED_STATUS == 503
    assert policy.PAUSED_BODY == {"error": "Paused. Send a message to wake it.",
                                  "code": "paused"}


def test_the_pages_fallback_sentence_is_the_gateways_sentence():
    """main.js falls back to its own copy of the sentence when a reply has a
    code and no error text. Two copies that disagree show a tenant one
    sentence on a good day and another on a bad one.

    Comments are stripped first. The sentence is quoted in a comment a few
    lines further down, so the file-wide check this replaces passed with the
    fallback itself already changed."""
    sentence = policy.PAUSED_BODY["error"]
    code = _code_only(MAIN_JS.read_text(encoding="utf-8"))
    assert sentence in code, (
        f"main.js does not fall back to the gateway's sentence {sentence!r}. "
        "The page shows its own copy whenever a paused reply carries no error "
        "text, so the two have to be the same string.")


def test_the_background_header_name_is_the_one_the_page_sends():
    """A3 marks a timer-driven request with this; the gateway classifies by
    it and not by path, because refresh() runs on a timer and after a user
    action and only the timer's call carries it."""
    assert policy.BACKGROUND_HEADER == "X-Waku-Background"
    assert (f'"{policy.BACKGROUND_HEADER}": "1"'
            in _code_only(MAIN_JS.read_text(encoding="utf-8")))
