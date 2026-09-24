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
for all of them. A `switch (body.code)`, an `includes(body.code)`, a
comparison against a template literal or against a named constant leaves a
token the scanner did not claim, and the test fails asking for the scanner to
be extended rather than passing while a branch goes unread. That is the A5
inversion: the closed set is the token, and the recognised forms are checked
against it. `body.code == "paused"` with two equals signs is NOT one of those:
CODE_COMPARISON is `===?` and reads it deliberately, as the comment on it says.
An earlier draft of this paragraph claimed otherwise, which overstated the
guard by one form.
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


def _codes_read_in(path: Path) -> set[str]:
    found: set[str] = set()
    for left, right in CODE_COMPARISON.findall(_code_only(path.read_text(encoding="utf-8"))):
        found.add(left or right)
    return found


def test_main_js_itself_still_reads_the_gateways_code():
    """The set test below ranges over the whole of js/, which is right for a
    set -- a hosted-specific file group D or E adds belongs in it. But a set
    over a directory can be satisfied from the wrong file: replace main.js's
    reader with `body.status === 503`, put `_decoy.code === "paused"` in
    util.js, and the set test is green with the only reader gone. Proved.

    So the file that actually handles the reply is named here, and only here.
    Nothing about WHICH code it reads is asserted -- that is the set test's
    job, and pinning it twice would mean two places to edit for one change."""
    assert MAIN_JS.is_file(), f"{MAIN_JS} is gone: this contract now guards nothing"
    assert _codes_read_in(MAIN_JS), (
        f"{MAIN_JS.name} no longer compares a `.code` against a string "
        "literal. A3's handleNotOk is what turns a hosted error body into the "
        "paused banner; either it was removed or it was rewritten in a shape "
        "the scanner below cannot read, and a `.code` somewhere else under "
        f"{JS_DIR.name}/ does not replace it.")


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
        found |= _codes_read_in(path)
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


def _bodies() -> dict[str, dict]:
    """Every hosted error body `policy.py` declares, by name.

    Enumerated from the module rather than listed here. A hand-written list
    fails CLOSED for a body wired into CODES -- you are forced to edit the
    line -- and fails OPEN for one that is not, which is the dangerous
    direction and the one this test exists to see. Proved: an
    AT_CAPACITY_BODY added to policy.py with no CODES entry and no page
    branch left all seven tests in this file green.

    The naming convention is the contract, and policy.py says so beside
    CODES: a hosted error body is a module-level dict whose name ends in
    _BODY. A body that hides under another name is invisible here -- which
    is why the convention is written down on the side that has to follow it
    rather than only on the side that checks it.
    """
    return {name: value for name, value in vars(policy).items()
            if name.endswith("_BODY") and isinstance(value, dict)}


def test_every_declared_code_is_actually_used_by_a_body():
    """CODES is the declaration; the *_BODY dicts are what actually goes on
    the wire. A code declared and never put in a body would satisfy the test
    above while nothing sends it, and a body carrying a code that is not
    declared is a reply the page has never heard of."""
    bodies = _bodies()
    assert bodies, (
        "policy.py declares no *_BODY dict. Either the hosted error bodies "
        "were removed, or they were renamed out of the convention this test "
        "and policy.py's own comment beside CODES both rely on.")
    codeless = sorted(name for name, body in bodies.items() if "code" not in body)
    assert not codeless, (
        f"hosted error bodies with no `code` field: {codeless}. The page "
        "branches on `code`, so a body without one can only fall through.")
    assert {body["code"] for body in bodies.values()} == set(policy.CODES), (
        f"codes carried by a body but not declared in CODES: "
        f"{sorted({b['code'] for b in bodies.values()} - set(policy.CODES))}\n"
        f"codes declared in CODES that no body carries: "
        f"{sorted(set(policy.CODES) - {b['code'] for b in bodies.values()})}\n"
        f"bodies read: {sorted(bodies)}")


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
