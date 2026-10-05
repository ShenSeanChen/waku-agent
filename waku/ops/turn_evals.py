"""Turn evals: checks that grade a person's own turns (spec 015).

The Evals page used to grade only the Waku Agent codebase. These checks grade
one turn at a time, from the trace the agent already wrote:

- Five **code checks** are pure functions of one turn's trace events. Each
  answers pass (1), fail (0) or n/a (None) with a one-line note. The page runs
  them every time it reads the traces (`observability.build_turn`), so an old
  turn is graded the first time the page opens, and a fixed check re-grades
  every turn at once. Nothing is written to disk for a code check.
- One **AI-judge check** runs only when a person presses "Judge this turn".
  It asks the small model of the provider in use whether the reply answers
  the question and stays grounded in the tool outputs. Its answer costs money
  and can differ twice, so it is written once, as a `score` trace event.

A check grades a finished turn. It never stops, rewrites or delays a reply.

The five checks, in the order the page lists them (TURN_CHECKS):

  spend_claim       the reply's spend matches the trace
  one_report        a research turn saves exactly one report
  grounded_numbers  the numbers in the reply come from a tool, memory or the message
  errors_handled    every failed tool call was retried or mentioned
  under_budget      the turn cost less than WAKU_TURN_BUDGET_USD (default $1.00)

PRIVACY. The checks read this home's own traces and send nothing anywhere.
The judge sends one turn's message, reply and tool outputs to the provider
the person already uses for every turn, and only when they press the button.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from waku.ops import observability as obs
from waku.ops.pricing import price_for

PASS, FAIL, NA = 1, 0, None
DEFAULT_BUDGET_USD = 1.00
BUDGET_ENV = "WAKU_TURN_BUDGET_USD"
GROUNDED_SHARE = 0.9          # grounded_numbers passes when 90% of the numbers are found
GROUNDED_MIN_NUMBERS = 3      # below this many numbers the check is n/a
SPEND_TOLERANCE_USD = 0.01    # a claimed treg figure may be off by this much...
SPEND_TOLERANCE_SHARE = 0.20  # ...or by this share of the real figure
JUDGE_PASS = 0.7              # a judge score passes at 0.7

# What each check answers, as the Evals page and `waku evals turns` print it.
TURN_CHECKS = {
    "spend_claim": "the reply's spend matches the trace's treg dollars",
    "one_report": "a research turn saves exactly one report",
    "grounded_numbers": "the reply's numbers appear in a tool output, a memory or the message",
    "errors_handled": "every failed tool call was retried or mentioned in the reply",
    "under_budget": f"the turn cost less than the per-turn budget ({BUDGET_ENV})",
}

# ---------------------------------------------------------------------------
# One turn, as the checks read it
# ---------------------------------------------------------------------------


def budget_usd() -> float:
    """The per-turn budget: WAKU_TURN_BUDGET_USD, or $1.00 when it is unset
    or not a positive number."""
    try:
        value = float(os.getenv(BUDGET_ENV, "") or DEFAULT_BUDGET_USD)
    except ValueError:
        return DEFAULT_BUDGET_USD
    return value if value > 0 else DEFAULT_BUDGET_USD


_SKILLS: dict[str, object] = {}


def skill_loader(home: Path | None = None):
    """The skills a turn's message is matched against: the bundled ones and
    this home's own, the same set `brain.is_research()` is given."""
    from waku.memory import bundled_skill_dirs  # noqa: PLC0415 -- heavy, and only needed here
    from waku.memory.procedural.loader import SkillLoader  # noqa: PLC0415

    key = str(home or "")
    if key not in _SKILLS:
        dirs = [*bundled_skill_dirs(), *([home / "skills"] if home else [])]
        _SKILLS[key] = SkillLoader(dirs)
    _SKILLS[key].match("")   # reloads once here if a skill changed, not once per turn
    return _SKILLS[key]


def is_research(message: str, skills=None) -> bool:
    """True when the research-report skill matches the message: the rule
    `brain.is_research()` applies before a turn starts."""
    from waku.memory import brain  # noqa: PLC0415

    return brain.is_research((skills or skill_loader()).match(message or "", rescan=False))


def view(turn: dict, *, usd: float | None = None, skills=None, servers=()) -> dict:
    """What the checks read from one grouped turn (`observability.group_turns`):
    the message, the reply, each tool call with its whole output, the report
    events, the turn's dollars and whether the trace is v2 (it has a turn id,
    so its tool outputs are kept whole and its reports are traced)."""
    tools = []
    for ev in turn.get("events") or []:
        if ev.get("type") != "tool":
            continue
        info = obs.describe(ev, servers)
        tools.append({"tool": ev.get("tool") if isinstance(ev.get("tool"), str) else "",
                      "args": ev.get("args"),
                      "output": ev.get("output") if isinstance(ev.get("output"), str) else "",
                      "ok": info["ok"], "source": info["source"], "cost_usd": info.get("cost_usd"),
                      "endpoint_id": info.get("endpoint_id") or "", "provider": info.get("provider") or ""})
    message = str(turn.get("user_message") or "")
    return {"message": message, "reply": str(turn.get("reply") or ""), "tools": tools,
            "reports": [str(ev.get("memory_id") or "") for ev in turn.get("events") or []
                        if ev.get("type") == "report"],
            "usd": usd, "v2": bool(turn.get("turn_id")), "ts": obs._ts(turn.get("ts")),
            "research": is_research(message, skills), "unfinished": bool(turn.get("unfinished"))}


def _result(name: str, value, note: str) -> dict:
    return {"source": "code", "name": name, "value": value, "note": note}


def money(usd: float) -> str:
    """$0.00 for nothing, four places under a cent ($0.0089), else two."""
    if not usd:
        return "$0.00"
    return f"${usd:.4f}" if abs(usd) < 0.01 else f"${usd:,.2f}"


# ---------------------------------------------------------------------------
# Reading a reply
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_URL = re.compile(r"\(?https?://\S+")
_MARKUP = re.compile(r"[*_~]{1,3}")


def prose(reply: str) -> str:
    """The reply without its code blocks, inline code, links and emphasis."""
    text = _FENCE.sub(" ", reply or "")
    text = _INLINE_CODE.sub(" ", text)
    text = _URL.sub(" ", text)
    return _MARKUP.sub("", text)


def sentences(text: str) -> list[str]:
    """Sentences and list items: split at line breaks and after . ! ? before a space."""
    out = []
    for line in text.splitlines():
        out += [s.strip() for s in re.split(r"(?<=[.!?])\s+", line) if s.strip()]
    return out


# ---------------------------------------------------------------------------
# 1. spend_claim
# ---------------------------------------------------------------------------

# A dollar figure, but not a scaled one: "$10M" is a funding round, never
# what a few tool calls cost.
_DOLLARS = re.compile(r"\$\s?(\d+(?:,\d{3})*(?:\.\d+)?)(?!\d|,\d|\.\d)"
                      r"(?!\s?(?:[kmb]|bn|mn|thousand|million|billion)\b)",
                      re.IGNORECASE)
# "free" or "no cost" says what something cost; "nothing" and "zero" only
# do beside a spend word ("cost nothing"), never alone ("lists nothing
# dated after 2024-11-20"), and a "free tier" or "free preview" names a
# product, not this turn's price.
_ZERO = re.compile(r"(?<!feel )\bfree\b(?![ -](?:tier|plan|trial|version|account|text|form|preview|credits?)\b)|\bno (?:cost|charge)\b|"
                   r"\b(?:cost|costs|spent|spend|billed|charged|paid)\s+(?:us\s+|me\s+)?(?:nothing|zero)\b|"
                   r"\$\s?0(?:\.0+)?\b(?!\.\d)", re.IGNORECASE)
_SPEND_WORD = re.compile(r"\b(cost|costs|spent|spend|billed|charged|total|paid)\b", re.IGNORECASE)
# Without a spend word, a sentence naming a provider states its price only
# with one of these ("LeadsForge's free preview ($0.00)").
_PRICE_WORD = re.compile(r"\b(free|paid|price|priced|fee|charge)\b", re.IGNORECASE)
# A price offered for later is not a claim about this turn.
_LATER = re.compile(r"\b(would|will|that'd|that would|each|per row|per call|per result|if)\b|'ll\b",
                    re.IGNORECASE)
_FUTURE = re.compile(r"\b(would|will|that'd|that would|if)\b|'ll\b", re.IGNORECASE)
_COUNTS = {w: i for i, w in enumerate(
    ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
     "eleven", "twelve"))}
# "four Aviato calls at $0.01 each" states $0.04: a count times a unit price
_TIMES = re.compile(
    r"\b(\d+|" + "|".join(_COUNTS) + r")\s+(?:[\w.'-]+\s+){0,3}?"
    r"(?:calls?|requests?|lookups?|queries|searches|pulls?|rows|results|records)\s+"
    r"(?:at|for)\s+(?:about\s+|~)?\$\s?(\d[\d,]*(?:\.\d+)?)\s*(?:each|apiece|per\s+\w+|a\s+call)\b",
    re.IGNORECASE)


def _treg(tools: list[dict]) -> list[dict]:
    return [t for t in tools if t["source"] == "treg" and t["cost_usd"] is not None]


def _named(sentence: str, tools: list[dict]) -> list[dict]:
    """The treg calls whose provider or endpoint the sentence names."""
    low = sentence.lower()
    out = []
    for t in tools:
        names = {t["provider"].lower(), t["endpoint_id"].lower()} - {""}
        if any(re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", low) for n in names):
            out.append(t)
    return out


def _amounts(sentence: str) -> tuple[list[float], list[str]] | None:
    """(the dollar figures the sentence states for this turn, as written), or
    None when it only offers a price for later. "N calls at $X each" states
    N times X; any other "each" or "per call" is a price, not a spend."""
    times = list(_TIMES.finditer(sentence))
    if times and not _FUTURE.search(sentence):
        rest = _TIMES.sub(" ", sentence)
        out, said = [], []
        for x in times:
            count = x.group(1).lower()
            n = int(count) if count.isdigit() else _COUNTS[count]
            out.append(round(n * float(x.group(2).replace(",", "")), 6))
            said.append(money(out[-1]))
        for m in _DOLLARS.finditer(rest):
            out.append(float(m.group(1).replace(",", "")))
            said.append(m.group(0).replace(" ", ""))
        return out, said
    if _LATER.search(sentence):
        return None
    found = list(_DOLLARS.finditer(sentence))
    return ([float(m.group(1).replace(",", "")) for m in found],
            [m.group(0).replace(" ", "") for m in found])


def check_spend_claim(v: dict) -> dict:
    """The reply's spend matches the trace. A sentence states this turn's
    spend when it carries a dollar amount, "free" or "no cost" beside a spend
    word ("cost", "spent", "billed", "charged", "total"), or names a treg
    provider or endpoint of this turn beside a price word ("free", "paid").
    "N calls at $X each" states N times X; any other price offered for later
    ("would", "each", "that'd") is skipped, and so is a scaled figure ("$10M")
    and "nothing" or "zero" away from a spend word. A sentence that names a
    provider is checked against that provider's own calls; any other is
    checked against the turn's treg dollars. It fails when the reply claims
    $0, "free" or "no cost" while the calls cost more, or a figure off by
    more than $0.01 and 20%. n/a when the reply claims nothing."""
    calls = _treg(v["tools"])
    claims = 0
    for sentence in sentences(prose(v["reply"])):
        stated = _amounts(sentence)
        if stated is None:
            continue
        amounts, written = stated
        zero = bool(_ZERO.search(sentence)) and not any(amounts)
        named = _named(sentence, calls)
        if not (amounts or zero):
            continue
        if not (_SPEND_WORD.search(sentence) or (named and _PRICE_WORD.search(sentence))):
            continue
        scope = named or calls
        actual = round(sum(t["cost_usd"] for t in scope), 6)
        whose = ("the " + ", ".join(sorted({t["provider"] or t["endpoint_id"] for t in named})) + " calls"
                 if named else "this turn's treg calls")
        # a figure for one provider, or for treg, is only checked when the
        # sentence says whose it is; an unscoped non-zero figure may be the
        # turn's whole total, which the reply is told not to give
        if not named and not zero and not re.search(r"\btreg\b", sentence, re.IGNORECASE):
            continue
        claims += 1
        said = written[0] if written else "free"
        if zero or all(a == 0 for a in amounts):
            if actual > 0:
                return _result("spend_claim", FAIL, f"reply says {said}; {whose} cost {money(actual)}")
            continue
        if not any(abs(a - actual) <= SPEND_TOLERANCE_USD or abs(a - actual) <= SPEND_TOLERANCE_SHARE * actual
                   for a in amounts):
            return _result("spend_claim", FAIL, f"reply says {said}; {whose} cost {money(actual)}")
    if not claims:
        return _result("spend_claim", NA, "the reply states no spend")
    return _result("spend_claim", PASS, f"{claims} spend claim{'s' if claims != 1 else ''} match the trace")


# ---------------------------------------------------------------------------
# 2. one_report
# ---------------------------------------------------------------------------


_SAVE_ASK = re.compile(r"\b(save|saves|saving|store|storing)\b", re.IGNORECASE)
# A body the model sent to memory_remember that is a report without the
# marker: a "# " title and "## " sections, filed in the Company brain.
_REPORT_SHAPE = re.compile(r"(?:^|\\n|\n)# \S.*?(?:\\n|\n)## \S", re.DOTALL)
# A message that asks for no report: "Just answer, no report", "don't save
# it", "skip the report". Such a turn is not owed one.
_NO_REPORT = re.compile(
    r"\b(?:no|without(?: an?| any)?|skip(?: the)?|(?:don't|do not|dont) (?:need|want)(?: an?| any)?)\s+"
    r"(?:report|reports|brief|snapshot|saving)\b|"
    r"\b(?:don't|do not|dont|never)\s+(?:save|store)\b|"
    r"\bjust (?:answer|tell me|reply)\b", re.IGNORECASE)


def _report_saves(v: dict) -> list[str]:
    """One entry per report this turn saved, deduplicated by memory id: each
    `report` event (the harness's save, which names the model's own memory
    when the model saved it first), and each memory_remember call that
    Waku Memory took whose body is a report (it holds the marker, or a "# "
    title with "## " sections filed in the Company brain)."""
    from waku.memory.consolidation import COMPANY_PROJECT  # noqa: PLC0415
    from waku.memory.reports import MARKER, REFUSAL  # noqa: PLC0415

    saves = [r or f"report-{i}" for i, r in enumerate(v["reports"])]
    for i, t in enumerate(v["tools"]):
        if not t["tool"].endswith("_memory_remember") or not t["ok"] or t["output"].startswith(REFUSAL[:20]):
            continue
        args = t["args"]
        body = args.get("body") if isinstance(args, dict) else args
        text = body if isinstance(body, str) else ""
        whole = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args or "")
        if not (MARKER in text or (COMPANY_PROJECT in whole and _REPORT_SHAPE.search(text))):
            continue
        try:
            memory_id = str(json.loads(t["output"])["memory"]["id"])
        except (ValueError, KeyError, TypeError):
            memory_id = f"remember-{i}"
        if memory_id not in saves:
            saves.append(memory_id)
    return saves


def check_one_report(v: dict) -> dict:
    """A research turn saves exactly one report; any turn that saves two
    fails. A save is a `report` event or a memory_remember call whose body
    is a report, counted once per memory. A turn is research when the
    research-report skill matches its message and the turn did research:
    a tool other than Waku Memory ran, or the message asks to save. A
    question answered from memory alone is n/a, and so is a turn whose
    message asks for no report ("Just answer, no report", "don't save");
    saving one anyway fails. With no Waku Memory
    connected no `report` event is written and the reply keeps the report,
    so a reply carrying the report marker counts as one. An older trace (no
    turn id) did not trace reports: n/a."""
    from waku.memory.reports import MARKER  # noqa: PLC0415

    saves = _report_saves(v)
    if len(saves) > 1:
        return _result("one_report", FAIL, f"{len(saves)} reports saved in one turn")
    if not v["research"]:
        return _result("one_report", NA, "not a research turn")
    if not v["v2"]:
        return _result("one_report", NA, "older trace: reports were not traced")
    if _NO_REPORT.search(v["message"]):
        if saves:
            return _result("one_report", FAIL, "the message asked for no report and one was saved")
        return _result("one_report", NA, "the message asked for no report")
    if len(saves) == 1:
        return _result("one_report", PASS, "one report saved")
    if MARKER in v["reply"]:
        return _result("one_report", PASS, "the report is in the reply; no Waku Memory to save it to")
    if not _SAVE_ASK.search(v["message"]) and all(t["source"] == "waku_memory" for t in v["tools"]):
        return _result("one_report", NA, "answered from memory: no research tool ran and no save was asked")
    return _result("one_report", FAIL, "a research turn with no report saved and none in the reply")


# ---------------------------------------------------------------------------
# 3. grounded_numbers
# ---------------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})")
_MDY = re.compile(rf"\b{_MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.IGNORECASE)
_DMY = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH}\s+(\d{{4}})\b", re.IGNORECASE)
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6,
          "b": 1e9, "bn": 1e9, "billion": 1e9}
_NUMBER = re.compile(
    r"(?<![\w.$-])(?P<money>\$)?\s?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<pct>\s?%)?(?:\s?(?P<scale>thousand|million|billion|bn|mn|[kmb])\b)?(?![\w.-]*\w)",
    re.IGNORECASE)
_ORDINAL = re.compile(r"^\d+(st|nd|rd|th)\b", re.IGNORECASE)
_LIST_NUMBER = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)


def _dates(text: str) -> list[tuple[str, int, int]]:
    """(ISO date, start, end) for every date written as 2026-10-05,
    Oct 5, 2026 or 5 October 2026."""
    out = [(f"{y}-{m}-{d}", x.start(), x.end()) for x in _ISO_DATE.finditer(text)
           for y, m, d in [x.groups()]]
    for x in _MDY.finditer(text):
        out.append((f"{x.group(3)}-{_MONTHS[x.group(1).lower()[:3]]:02d}-{int(x.group(2)):02d}",
                    x.start(), x.end()))
    for x in _DMY.finditer(text):
        out.append((f"{x.group(3)}-{_MONTHS[x.group(2).lower()[:3]]:02d}-{int(x.group(1)):02d}",
                    x.start(), x.end()))
    return out


def _numbers(text: str) -> list[tuple[str, float, int, int, bool]]:
    """(as written, value, decimals, start, is_money_or_percent) for each number."""
    out = []
    for x in _NUMBER.finditer(text):
        raw = x.group("num")
        value = float(raw.replace(",", ""))
        scale = (x.group("scale") or "").lower()
        if scale:
            value *= _SCALE[scale]
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        out.append((x.group(0).strip(), value, decimals, x.start(),
                    bool(x.group("money") or x.group("pct") or scale)))
    return out


def _reply_numbers(reply: str, today: datetime | None = None) -> list[tuple]:
    """What grounded_numbers looks for: each money amount, percentage, date
    and number of two or more digits in the reply's prose, as (as written,
    ISO date) or (as written, values, decimals, scale). Ordinals, list
    numbers and code blocks are skipped, and so is the turn's own date, a day
    either side ("the 2026-10-05 snapshot"): it says when, not what was found."""
    text = _LIST_NUMBER.sub(" ", _DASHES.sub("-", prose(reply)))
    near = ({(today.date() + timedelta(days=d)).isoformat() for d in (-1, 0, 1)}
            if today else set())
    wanted: list = []
    taken: list[tuple[int, int]] = []
    for iso, start, end in _dates(text):
        taken.append((start, end))
        if iso not in near:
            wanted.append((text[start:end], iso))
    for written, value, decimals, start, marked in _numbers(text):
        if any(a <= start < b for a, b in taken) or _ORDINAL.match(text[start:]):
            continue
        digits = sum(c.isdigit() for c in written)
        if not marked and digits < 2:
            continue
        mantissa, scale = value, 1.0
        suffix = re.search(r"(thousand|million|billion|bn|mn|[kmb])$", written, re.IGNORECASE)
        if suffix:
            scale = _SCALE[suffix.group(1).lower()]
            mantissa = value / scale
        values = [value, mantissa]
        if "%" in written:
            values.append(value / 100)
        wanted.append((written, values, decimals, scale))
    return wanted


_DASHES = re.compile("[\u2010\u2011\u2012\u2013\u2014\u2212]")


def _strings(value, depth: int = 0) -> list[str]:
    """Every string inside a decoded JSON value. A string that is itself
    JSON (an MCP text block, a relayed body kept as text) is decoded too, a
    few levels deep, and so are numbers kept as numbers."""
    if isinstance(value, str):
        if depth < 3 and value.lstrip()[:1] in ("{", "["):
            try:
                return [value, *_strings(json.loads(value), depth + 1)]
            except ValueError:
                pass
        return [value]
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return [s for v in value for s in _strings(v, depth)]
    return []


def _source_index(texts: list[str]) -> tuple[set[float], set[str]]:
    """The numbers and dates in a turn's sources. A JSON source (a Waku
    Memory answer, a treg result) is read decoded too, so a memory body sent
    as "2024\\u201108\\u201101" reads as 2024-08-01."""
    numbers: set[float] = set()
    dates: set[str] = set()
    decoded = []
    for text in texts:
        try:
            decoded += _strings(json.loads(text))
        except (ValueError, TypeError):
            pass
    for text in [*texts, *decoded]:
        text = _DASHES.sub("-", text)
        dates.update(iso for iso, _, _ in _dates(text))
        numbers.update(value for _, value, _, _, _ in _numbers(text))
        # a bare scaled figure in a source ("4.2M") also stands for its mantissa
        numbers.update(float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*\.?\d*", text) if n[-1].isdigit())
    return numbers, dates


def _found(values: list[float], decimals: int, numbers: set[float], scale: float = 1.0) -> bool:
    for value in values:
        if value in numbers:
            return True
    # "$0.01" is grounded by a source's 0.0089 when it rounds to it
    for n in numbers:
        if any(round(n, decimals) == round(v, decimals) and abs(n - v) < 10 ** -decimals for v in values):
            return True
    # "$1.58M" is grounded by a source's raw 1575000 or 1580000: the source,
    # in the reply's unit, rounds to what the reply wrote
    if scale > 1:
        half = 0.5 * 10 ** -decimals + 1e-9
        return any(abs(n / scale - values[1]) <= half for n in numbers if n >= scale / 10)
    return False


def _spend_figures(tools: list[dict]) -> set[float]:
    """What the trace says this turn's treg calls cost, each and in total:
    "the call cost $0.04" is grounded by the trace even when the call's own
    output does not repeat its price. A treg tool is `treg_call` (the
    own-key surface) or a `treg_catalog_call_*` relay; both carry cost_usd."""
    costs = [t["cost_usd"] for t in _treg(tools)]
    return {round(c, 6) for c in costs} | ({round(sum(costs), 6)} if costs else set())


def check_grounded_numbers(v: dict) -> dict:
    """Every money amount, percentage, date and number of two or more digits
    in the reply appears in a tool output, a Waku Memory result or the user's
    message of this turn, after normalising the format ($0.04 and 0.04, 1,200
    and 1200, $1.58M and 1580000, 275K and 275,000, Oct 5, 2026 and
    2026-10-05). The turn's own date is not a claim. Passes at 90% found; the
    note names up to three that were not. n/a below three numbers, for an
    older trace (no turn id), whose tool outputs may have been cut, and when
    a number is missing from a turn that ran no tool but Waku Memory: such a
    reply may quote the chat's earlier turns or recalled facts, which the
    trace does not keep."""
    if not v["v2"]:
        return _result("grounded_numbers", NA, "older trace: tool outputs may be cut")
    wanted = _reply_numbers(v["reply"], v.get("ts"))
    if len(wanted) < GROUNDED_MIN_NUMBERS:
        return _result("grounded_numbers", NA, f"{len(wanted)} number(s) in the reply; the check needs 3")
    numbers, dates = _source_index([v["message"], *(t["output"] for t in v["tools"])])
    numbers |= _spend_figures(v["tools"])
    missing = []
    for item in wanted:
        if isinstance(item[1], str):
            ok = item[1] in dates
        else:
            ok = _found(item[1], item[2], numbers, item[3])
        if not ok:
            missing.append(item[0])
    found = len(wanted) - len(missing)
    value = PASS if found / len(wanted) >= GROUNDED_SHARE else FAIL
    note = f"{found} of {len(wanted)} numbers found in the turn's sources"
    if missing:
        note += "; not found: " + ", ".join(missing[:3])
    if value == FAIL and all(t["source"] == "waku_memory" for t in v["tools"]):
        return _result("grounded_numbers", NA,
                       note + "; answered from memory, and the chat history and recalled facts are not traced")
    return _result("grounded_numbers", value, note)


# ---------------------------------------------------------------------------
# 4. errors_handled
# ---------------------------------------------------------------------------

_FAILURE_WORDS = re.compile(
    r"\b(error|errors|failed|fail|fails|failure|couldn't|could not|can't|cannot|didn't work|did not work|"
    r"unavailable|timed out|time out|unable|skipped|skip|insufficient|not enough|blocked|refused|rejected|"
    r"denied|wasn't able|was not able|weren't able|were not able)\b|"
    r"\b(?:did not|didn't|does not|doesn't|could not|couldn't|was not|wasn't|were not|weren't|"
    r"is not|isn't|are not|aren't|not)\s+(?:\w+\s+)?(?:run|ran|load|loaded|pulled|fetched|returned|"
    r"available|reachable|complete|completed|finish|finished|go through|went through)\b|"
    r"\bno\s+(?:\w+\s+){0,2}(?:was|were)\s+(?:run|pulled|fetched|returned|loaded|found|available)\b|"
    r"\bbelow\b.{0,60}\b(?:needed|required|minimum)\b", re.IGNORECASE)
# Endpoint parts too generic to stand for the failed call ("search", "get").
_GENERIC_PARTS = {"search", "list", "lookup", "info", "data", "details", "detail", "fetch", "query",
                  "call", "read", "web", "api", "app", "v1", "v2", "v3", "get", "post", "posts",
                  "user", "users", "companies", "company", "people", "person", "profile", "profiles",
                  "preview", "similar", "general", "items", "item", "results"}


def _tool_names(t: dict) -> set[str]:
    """What a reply may call a tool: its name, its short name, its provider,
    its endpoint, and each specific part of the endpoint ("tikhub.youtube.
    search_video" is "youtube" and "search video" too)."""
    name = t["tool"]
    short = name
    for prefix in (obs.TREG_PREFIX, obs.WAKU_MEMORY_PREFIX):
        short = short.removeprefix(prefix)
    parts = {p.replace("_", " ") for p in re.split(r"[./:]", t["endpoint_id"])
             if len(p) >= 4 and p.lower() not in _GENERIC_PARTS}
    # `treg_call` is short for nothing a reply would say: "call" is any call
    names = (name, short, short.replace("_", " "), t["provider"], t["endpoint_id"], *parts)
    return {n.lower() for n in names if n and n.lower() not in _GENERIC_PARTS}


def check_errors_handled(v: dict) -> dict:
    """Every failed tool call was retried or mentioned: a later call to the
    same tool in the turn succeeded, or the reply names the tool, its
    provider, its endpoint or a part of it ("YouTube" for tikhub.youtube.*),
    or says a step failed or did not happen ("error", "failed", "couldn't",
    "did not run", "were not pulled", "skipped", "below the ... needed").
    n/a when no tool failed."""
    tools = v["tools"]
    failed = [i for i, t in enumerate(tools) if not t["ok"]]
    if not failed:
        return _result("errors_handled", NA, "no tool call failed")
    reply = prose(v["reply"]).lower()
    said_failure = bool(_FAILURE_WORDS.search(reply))
    silent = []
    for i in failed:
        t = tools[i]
        retried = any(later["tool"] == t["tool"] and later["ok"] for later in tools[i + 1:])
        named = any(re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", reply) for n in _tool_names(t))
        if not (retried or named or said_failure):
            silent.append(t["tool"])
    if silent:
        return _result("errors_handled", FAIL,
                       f"{', '.join(dict.fromkeys(silent))} failed and the reply never says so")
    return _result("errors_handled", PASS,
                   f"{len(failed)} failed call{'s' if len(failed) != 1 else ''} retried or mentioned")


# ---------------------------------------------------------------------------
# 5. under_budget
# ---------------------------------------------------------------------------


def check_under_budget(v: dict) -> dict:
    """The turn's dollars (the receipt's charged total on hosted, the
    estimate on a laptop) are at most the per-turn budget."""
    usd, budget = v["usd"], budget_usd()
    if usd is None:
        return _result("under_budget", NA, "no cost recorded")
    if usd > budget:
        return _result("under_budget", FAIL, f"{money(usd)} is over the {money(budget)} budget")
    return _result("under_budget", PASS, f"{money(usd)} of the {money(budget)} budget")


CHECKS = {
    "spend_claim": check_spend_claim,
    "one_report": check_one_report,
    "grounded_numbers": check_grounded_numbers,
    "errors_handled": check_errors_handled,
    "under_budget": check_under_budget,
}


def run_checks(turn: dict, *, usd: float | None = None, skills=None, servers=()) -> list[dict]:
    """The five code checks on one grouped turn, as scores with source
    `code`: value 1 (pass), 0 (fail) or None (n/a). A check that raises is
    recorded as n/a with the note "check error" and never breaks the page."""
    try:
        v = view(turn, usd=usd, skills=skills, servers=servers)
    except Exception:
        return [_result(name, NA, "check error") for name in CHECKS]
    out = []
    for name, check in CHECKS.items():
        try:
            out.append(check(v))
        except Exception:
            out.append(_result(name, NA, "check error"))
    return out


# ---------------------------------------------------------------------------
# Your turns: the Evals page's table
# ---------------------------------------------------------------------------


def your_turns(turns: list[dict], window: str, now: datetime | None = None) -> dict:
    """One row per check for the turns in a window: passed, failed, n/a, the
    pass rate, and the newest failing turn. `turns` are built turns
    (`observability.build_turn`), oldest first. The judge row counts the
    turns judged and their average score."""
    start = obs.window_start(window, now)
    inside = [t for t in turns if obs._in_window(t, start)]
    rows = []
    for name, what in TURN_CHECKS.items():
        passed = failed = na = 0
        newest = None
        for t in inside:
            score = next((s for s in t.get("scores") or [] if s["source"] == "code" and s["name"] == name), None)
            value = score["value"] if score else None
            if value is None:
                na += 1
            elif value >= 1:
                passed += 1
            else:
                failed += 1
                newest = {"turn_id": t.get("turn_id") or "", "ts": t.get("ts"),
                          "user_message": t.get("user_message") or "", "note": score["note"]}
        scored = passed + failed
        rows.append({"name": name, "what": what, "passed": passed, "failed": failed, "na": na,
                     "pass_rate": passed / scored if scored else None, "newest_fail": newest})
    judged = [s for t in inside for s in t.get("scores") or [] if s["source"] == "judge"]
    graded = [[s["value"] for s in t.get("scores") or [] if s["source"] == "code" and s["value"] is not None]
              for t in inside]
    return {"window": window, "turns": len(inside), "checks": rows,
            # turns with at least one scored check, and those that passed every one
            "turns_scored": sum(1 for g in graded if g),
            "turns_passed": sum(1 for g in graded if g and all(v >= 1 for v in g)),
            "judge": {"judged": len(judged),
                      "average": round(sum(s["value"] for s in judged) / len(judged), 2) if judged else None,
                      "passed": sum(1 for s in judged if s["value"] >= JUDGE_PASS)}}


# ---------------------------------------------------------------------------
# The AI judge, on demand
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The AI judge, on demand
# ---------------------------------------------------------------------------

JUDGE_CHECK = "answer_grounded"
JUDGE_MAX_TOKENS = 300
JUDGE_REPEAT_SECONDS = 60
REPLY_CHARS = 4000
OUTPUT_CHARS = 800
OUTPUTS_CHARS = 6000

JUDGE_RUBRIC = """You are a strict, fair judge grading one turn of an AI assistant.

The person asked:
{message}

The assistant replied:
{reply}

The tool outputs the assistant saw this turn (each cut to {output_chars} characters):
{outputs}

Score two things from 0 to 10:
- addresses_question: does the reply answer what the person asked?
- grounded: is every fact, number and claim in the reply supported by the tool
  outputs above or the person's message? A claim the outputs contradict scores 0-3.

Reply with ONLY a JSON object, no prose:
{{"addresses_question": <int 0-10>, "grounded": <int 0-10>, "reason": "<one short sentence>"}}"""


def judge_prompt(turn: dict) -> str:
    """The judge's prompt, built from the stored turn only: its message, the
    first 4,000 characters of its reply, and its tool outputs, each cut to
    800 characters and together to 6,000."""
    outputs, used = [], 0
    for ev in turn.get("events") or []:
        if ev.get("type") != "tool" or used >= OUTPUTS_CHARS:
            continue
        text = obs.redact(str(ev.get("output") or ""))[:OUTPUT_CHARS]
        text = text[:OUTPUTS_CHARS - used]
        used += len(text)
        outputs.append(f"[{ev.get('tool') or 'tool'}] {text}")
    return JUDGE_RUBRIC.format(message=str(turn.get("user_message") or "")[:2000],
                               reply=str(turn.get("reply") or "")[:REPLY_CHARS],
                               output_chars=OUTPUT_CHARS,
                               outputs="\n".join(outputs) or "(no tools ran this turn)")


def judge_estimate(prompt: str, provider: str, model: str) -> float:
    """What one judge call should cost, from `pricing.py`: the prompt's
    characters divided by 4 as input tokens, plus 300 output tokens."""
    p_in, p_out = price_for(provider or "", model or "")
    return round(len(prompt) / 4 / 1e6 * p_in + JUDGE_MAX_TOKENS / 1e6 * p_out, 6)


def judge_offer(turn: dict, provider: str, model: str) -> dict | None:
    """What the "Judge this turn" button says before it is pressed: the
    model that would judge and its estimated cost. None for a turn the
    route would refuse (no turn id, no reply)."""
    if not turn.get("turn_id") or not str(turn.get("reply") or "").strip() or not model:
        return None
    return {"model": model, "usd": judge_estimate(judge_prompt(turn), provider, model)}


def parse_verdict(text: str) -> dict | None:
    """{"value", "reason", "addresses_question", "grounded"} from the judge's
    answer, or None when it holds no readable JSON. The score is the lower
    of the two marks divided by 10."""
    try:
        obj = json.loads(text[text.index("{"): text.rindex("}") + 1])
        a = max(0, min(10, int(obj["addresses_question"])))
        g = max(0, min(10, int(obj["grounded"])))
    except (ValueError, KeyError, TypeError):
        return None
    return {"value": round(min(a, g) / 10, 2), "reason": str(obj.get("reason") or "")[:200],
            "addresses_question": a, "grounded": g}


def judge_client(settings):
    """(client, model, provider) for the judge: the small model of the
    provider this home uses now. On the hosted free tier that is the
    `waku-platform` row, so the call goes through the metering proxy and is
    charged in Waku Memory credits; with a person's own key it is charged to
    that key."""
    from dataclasses import replace  # noqa: PLC0415

    from waku.loop.models import get_client  # noqa: PLC0415

    copy = replace(settings)
    client = get_client(copy)   # fills in the provider's default ids
    model = copy.small_model or copy.model
    return client, model, copy.provider


class JudgeRefused(Exception):
    """The judge route's answer for a turn it will not judge."""


_RECENT: dict[str, float] = {}
_RECENT_LOCK = threading.Lock()


def find_turn(home: Path, turn_id: str) -> tuple[dict | None, list[dict]]:
    """The grouped turn with this id from this home's traces, and every
    judge score already written for it."""
    events, _, _ = obs.read_events(home)
    turn = next((t for t in obs.group_turns(events) if t.get("turn_id") == turn_id), None)
    judged = [e for e in events if e.get("type") == "score" and e.get("turn_id") == turn_id
              and e.get("source") == "judge"]
    return turn, judged


def _claim(turn_id: str, judged: list[dict], now: float) -> None:
    """Refuse a second judge of the same turn within 60 seconds: one already
    running or finished in this process, or a score written that recently."""
    newest = max((obs._ts(e.get("ts")) for e in judged if obs._ts(e.get("ts"))), default=None)
    if newest is not None and now - newest.timestamp() < JUDGE_REPEAT_SECONDS:
        raise JudgeRefused("This turn was judged less than a minute ago.")
    with _RECENT_LOCK:
        if now - _RECENT.get(turn_id, 0) < JUDGE_REPEAT_SECONDS:
            raise JudgeRefused("This turn was judged less than a minute ago.")
        _RECENT[turn_id] = now


def judge_turn(home: Path, turn_id: str, client, model: str, provider: str,
               now: float | None = None) -> dict:
    """Judge one stored turn and write the result as one `score` trace event
    (source `judge`, joined by `turn_id`), with the model that judged and
    what the call cost. Raises JudgeRefused for an unknown turn, a turn with
    no reply, or a repeat within 60 seconds."""
    if not isinstance(turn_id, str) or not re.fullmatch(r"t_[0-9a-f]{4,32}", turn_id):
        raise JudgeRefused("No turn with that id in this home's traces.")
    turn, judged = find_turn(home, turn_id)
    if turn is None:
        raise JudgeRefused("No turn with that id in this home's traces.")
    if not str(turn.get("reply") or "").strip():
        raise JudgeRefused("This turn has no reply to judge.")
    now = time.time() if now is None else now
    _claim(turn_id, judged, now)
    prompt = judge_prompt(turn)
    # The judge's own id, so the metering proxy can answer this call's exact
    # charge (GET /v1/turns/<id>/charges) apart from the turn it judges.
    call_id = "j_" + secrets.token_hex(8)
    if hasattr(client, "turn_id"):
        client.turn_id = call_id
    try:
        resp = client.messages.create(model=model, max_tokens=JUDGE_MAX_TOKENS,
                                      messages=[{"role": "user", "content": prompt}])
    except Exception as exc:
        with _RECENT_LOCK:
            _RECENT.pop(turn_id, None)   # a failed call may be retried at once
        raise JudgeRefused(f"The judge could not be reached: {type(exc).__name__}.") from exc
    finally:
        if hasattr(client, "turn_id"):
            client.turn_id = ""
    text = "".join(getattr(b, "text", "") for b in getattr(resp, "content", []) or []
                   if getattr(b, "type", "") == "text")
    usage = getattr(resp, "usage", None)
    n_in, n_out = int(getattr(usage, "input_tokens", 0) or 0), int(getattr(usage, "output_tokens", 0) or 0)
    cost = obs.llm_cost(provider, model, {"in": n_in, "out": n_out})
    charged = client.charges(call_id) if hasattr(client, "charges") else None
    if isinstance(charged, dict) and isinstance(charged.get("model_usd"), int | float):
        cost = round(float(charged["model_usd"]), 6)
    _record_usage(home, provider, model, call_id, n_in, n_out)
    verdict = parse_verdict(text)
    if verdict is None:
        raise JudgeRefused(f"The judge answered without a readable score ({money(cost)} spent).")
    event = {"type": "score", "turn_id": turn_id, "source": "judge", "name": JUDGE_CHECK,
             "check": JUDGE_CHECK, "value": verdict["value"], "judge": model, "cost_usd": cost,
             "note": f"{verdict['reason']} (judged by {model}, {money(cost)})",
             "addresses_question": verdict["addresses_question"], "grounded": verdict["grounded"]}
    write_score(home, event)
    return event


def write_score(home: Path, event: dict) -> None:
    """Append one `score` event to today's trace file, as the tracer writes lines."""
    path = home / "traces" / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {**event, "ts": datetime.now(UTC).isoformat(timespec="milliseconds")}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obs.redact(record), ensure_ascii=False, default=str) + "\n")


def _record_usage(home: Path, provider: str, model: str, call_id: str, n_in: int, n_out: int) -> None:
    """The judge's tokens in the spend ledger (usage.jsonl), as kind
    "judge", so the Spend tab counts every model call this home paid for."""
    record = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "provider": provider,
              "model": model, "kind": "judge", "turn_id": call_id, "in": n_in, "out": n_out}
    with (home / "usage.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# waku evals turns
# ---------------------------------------------------------------------------


def report(home: Path | None, window: str = "7d", now: datetime | None = None) -> str:
    """The `waku evals turns` text: the Your turns table, then every failing
    turn with the check that failed and its note."""
    from waku.config import load_settings  # noqa: PLC0415

    window = window if window in obs.WINDOWS else "7d"
    settings = load_settings() if home is None else None
    home = home or settings.home
    events, _, _ = obs.read_events(home)
    servers = obs.mcp_servers(home)
    skills = skill_loader(home)
    late: dict[str, list] = {}
    for ev in events:
        if ev.get("type") == "score" and ev.get("turn_id"):
            late.setdefault(ev["turn_id"], []).append(ev)
    turns = [obs.build_turn(t, servers, scores=late.get(t.get("turn_id") or "", []), skills=skills)
             for t in obs.group_turns(events)]
    table = your_turns(turns, window, now)
    label = {"today": "today", "7d": "the last 7 days", "all": "all time"}[window]
    lines = [f"Your turns, {label}: {table['turns']} turn(s) in {home / 'traces'}", ""]
    lines.append(f"{'check':<18}{'passed':>8}{'failed':>8}{'n/a':>6}{'rate':>7}  what it checks")
    for row in table["checks"]:
        # floored, so 228 of 229 reads 99.5%, never 100%
        rate = f"{int(row['pass_rate'] * 1000) / 10:g}%" if row["pass_rate"] is not None else "-"
        lines.append(f"{row['name']:<18}{row['passed']:>8}{row['failed']:>8}{row['na']:>6}{rate:>7}  {row['what']}")
    j = table["judge"]
    lines.append(f"{'judge':<18}{j['judged']:>8} judged"
                 + (f", average {j['average']}" if j["average"] is not None else ""))
    start = obs.window_start(window, now)
    failing = [(t, s) for t in turns if obs._in_window(t, start)
               for s in t["scores"] if s["source"] == "code" and s["value"] == 0]
    lines += ["", f"Failing turns: {len(failing)}" if failing else "No failing turns."]
    for t, s in failing[::-1]:
        when = str(t.get("ts") or "")[:16].replace("T", " ")
        lines.append(f"  {when}  {t.get('turn_id') or '(older trace)'}  {s['name']}: {s['note']}")
        lines.append(f"    \"{(t.get('user_message') or '')[:80]}\"")
    return "\n".join(lines)


def cli_main(argv: list[str]) -> int:
    """`waku evals turns [--window today|7d|all]`."""
    window = "7d"
    if "--window" in argv:
        i = argv.index("--window")
        window = argv[i + 1] if i + 1 < len(argv) else ""
    if window not in obs.WINDOWS:
        print(f"--window takes one of: {', '.join(obs.WINDOWS)}")
        return 2
    print(report(None, window))
    return 0
