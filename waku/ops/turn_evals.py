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

import os
import re
from datetime import datetime
from pathlib import Path

from waku.ops import observability as obs

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
                      "output": ev.get("output") if isinstance(ev.get("output"), str) else "",
                      "ok": info["ok"], "source": info["source"], "cost_usd": info.get("cost_usd"),
                      "endpoint_id": info.get("endpoint_id") or "", "provider": info.get("provider") or ""})
    message = str(turn.get("user_message") or "")
    return {"message": message, "reply": str(turn.get("reply") or ""), "tools": tools,
            "reports": sum(1 for ev in turn.get("events") or [] if ev.get("type") == "report"),
            "usd": usd, "v2": bool(turn.get("turn_id")),
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

_DOLLARS = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")
_ZERO = re.compile(r"\b(free|no cost|nothing|zero)\b|\$\s?0(?:\.0+)?\b", re.IGNORECASE)
_SPEND_WORD = re.compile(r"\b(cost|costs|spent|spend|billed|charged|total|paid)\b", re.IGNORECASE)
# A price offered for later is not a claim about this turn.
_LATER = re.compile(r"\b(would|will|that'd|that would|each|per row|per call|per result|if)\b|'ll\b",
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


def check_spend_claim(v: dict) -> dict:
    """The reply's spend matches the trace. A sentence states this turn's
    spend when it carries a dollar amount, "free" or "no cost" beside a spend
    word ("cost", "spent", "billed", "charged", "total"), or names a treg
    provider or endpoint of this turn. A price offered for later ("would",
    "each", "that'd") is skipped. A sentence that names a provider is checked
    against that provider's own calls; any other is checked against the
    turn's treg dollars. It fails when the reply claims $0, "free" or "no
    cost" while the calls cost more, or a figure off by more than $0.01 and
    20%. n/a when the reply claims nothing."""
    calls = _treg(v["tools"])
    claims = 0
    for sentence in sentences(prose(v["reply"])):
        if _LATER.search(sentence):
            continue
        amounts = [float(a.replace(",", "")) for a in _DOLLARS.findall(sentence)]
        zero = bool(_ZERO.search(sentence)) and not any(amounts)
        named = _named(sentence, calls)
        if not (amounts or zero) or not (_SPEND_WORD.search(sentence) or named):
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
        said = next((m.group(0).replace(" ", "") for m in _DOLLARS.finditer(sentence)), "free")
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


def check_one_report(v: dict) -> dict:
    """A research turn saves exactly one report; any turn that saves two
    fails. With no Waku Memory connected no `report` event is written and the
    reply keeps the report, so a reply carrying the report marker counts as
    one. An older trace (no turn id) did not trace reports: n/a."""
    from waku.memory.reports import MARKER  # noqa: PLC0415

    if v["reports"] > 1:
        return _result("one_report", FAIL, f"{v['reports']} reports saved in one turn")
    if not v["research"]:
        return _result("one_report", NA, "not a research turn")
    if not v["v2"]:
        return _result("one_report", NA, "older trace: reports were not traced")
    if v["reports"] == 1:
        return _result("one_report", PASS, "one report saved")
    if MARKER in v["reply"]:
        return _result("one_report", PASS, "the report is in the reply; no Waku Memory to save it to")
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


def _reply_numbers(reply: str) -> list[tuple[str, list[float], int] | tuple[str, str]]:
    """What grounded_numbers looks for: each money amount, percentage, date
    and number of two or more digits in the reply's prose. Ordinals, list
    numbers and code blocks are skipped."""
    text = _LIST_NUMBER.sub(" ", prose(reply))
    wanted: list = []
    taken: list[tuple[int, int]] = []
    for iso, start, end in _dates(text):
        wanted.append((text[start:end], iso))
        taken.append((start, end))
    for written, value, decimals, start, marked in _numbers(text):
        if any(a <= start < b for a, b in taken) or _ORDINAL.match(text[start:]):
            continue
        digits = sum(c.isdigit() for c in written)
        if not marked and digits < 2:
            continue
        mantissa = value
        scale = re.search(r"(thousand|million|billion|bn|mn|[kmb])$", written, re.IGNORECASE)
        if scale:
            mantissa = value / _SCALE[scale.group(1).lower()]
        values = [value, mantissa]
        if "%" in written:
            values.append(value / 100)
        wanted.append((written, values, decimals))
    return wanted


def _source_index(texts: list[str]) -> tuple[set[float], set[str]]:
    numbers: set[float] = set()
    dates: set[str] = set()
    for text in texts:
        dates.update(iso for iso, _, _ in _dates(text))
        numbers.update(value for _, value, _, _, _ in _numbers(text))
        # a bare scaled figure in a source ("4.2M") also stands for its mantissa
        numbers.update(float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*\.?\d*", text) if n[-1].isdigit())
    return numbers, dates


def _found(values: list[float], decimals: int, numbers: set[float]) -> bool:
    for value in values:
        if value in numbers:
            return True
    # "$0.01" is grounded by a source's 0.0089 when it rounds to it
    for n in numbers:
        if any(round(n, decimals) == round(v, decimals) and abs(n - v) < 10 ** -decimals for v in values):
            return True
    return False


def check_grounded_numbers(v: dict) -> dict:
    """Every money amount, percentage, date and number of two or more digits
    in the reply appears in a tool output, a Waku Memory result or the user's
    message of this turn, after normalising the format ($0.04 and 0.04, 1,200
    and 1200, Oct 5, 2026 and 2026-10-05). Passes at 90% found; the note
    names up to three that were not. n/a below three numbers, and for an
    older trace (no turn id), whose tool outputs may have been cut."""
    if not v["v2"]:
        return _result("grounded_numbers", NA, "older trace: tool outputs may be cut")
    wanted = _reply_numbers(v["reply"])
    if len(wanted) < GROUNDED_MIN_NUMBERS:
        return _result("grounded_numbers", NA, f"{len(wanted)} number(s) in the reply; the check needs 3")
    numbers, dates = _source_index([v["message"], *(t["output"] for t in v["tools"])])
    missing = []
    for item in wanted:
        if isinstance(item[1], str):
            ok = item[1] in dates
        else:
            ok = _found(item[1], item[2], numbers)
        if not ok:
            missing.append(item[0])
    found = len(wanted) - len(missing)
    value = PASS if found / len(wanted) >= GROUNDED_SHARE else FAIL
    note = f"{found} of {len(wanted)} numbers found in the turn's sources"
    if missing:
        note += "; not found: " + ", ".join(missing[:3])
    return _result("grounded_numbers", value, note)


# ---------------------------------------------------------------------------
# 4. errors_handled
# ---------------------------------------------------------------------------

_FAILURE_WORDS = re.compile(
    r"\b(error|errors|failed|fail|failure|couldn't|could not|can't|cannot|didn't work|did not work|"
    r"unavailable|timed out|time out|unable)\b", re.IGNORECASE)


def _tool_names(t: dict) -> set[str]:
    name = t["tool"]
    short = name
    for prefix in (obs.TREG_PREFIX, obs.WAKU_MEMORY_PREFIX):
        short = short.removeprefix(prefix)
    return {n.lower() for n in (name, short, short.replace("_", " "), t["provider"], t["endpoint_id"]) if n}


def check_errors_handled(v: dict) -> dict:
    """Every failed tool call was retried or mentioned: a later call to the
    same tool in the turn succeeded, or the reply names the tool, its
    provider or endpoint, or says a step failed ("error", "failed",
    "couldn't"). n/a when no tool failed."""
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
        named = any(n in reply for n in _tool_names(t))
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
