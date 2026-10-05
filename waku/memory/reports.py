"""Research reports — a reply that holds one is saved to Waku Memory whole.

Spec 007. The research-report skill (skills/research-report/) teaches the
format, waku-report v1: one Markdown document whose first line is MARKER.
This module is the writer's half of that contract, and it does three things
once a turn has been answered:

  1. finds the report in the reply (MARKER on a line of its own),
  2. sends it to Waku Memory as one `semantic` memory, in the Company brain
     project when it is company or market research and `global` otherwise,
  3. hands back a short chat reply and a `report` event for the chat's card.

With no Waku Memory connected, or when the send fails, the reply is left
whole and there is no event: the report is never in neither place.

A turn saves at most one report. On 2026-10-05 one research turn left two
in the Company brain, eight seconds apart: the model saved the whole report
itself with waku_memory_memory_remember, then wrote a shorter one into its
reply, which this module saved too. The bodies differed, so Waku Memory's
own dedupe (same body, same scope) kept both. Now the model's own call is
refused (`guard_remember`), and a report the model did save this turn is the
turn's report: `save` sends nothing more and points the card at it.

Earlier reports are read as digests (`digest`): title, Summary and key
numbers. `shrink_read` cuts a whole report the model already read in an
earlier loop iteration to its digest, so it is not re-sent on every call
after it (spec 009 A). Like
consolidation, this module never sees the transport; app.py passes the same
`remember` callable spec 006 built, so a fake stands in for it in the evals.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

import anthropic

from waku.memory.consolidation import COMPANY_PROJECT

MARKER = "<!-- waku-report v1 -->"

# Waku Memory's kind for a report: waku.one files `semantic` under Knowledge,
# and a report is the thing another agent should recall whole.
KIND = "semantic"

SCOPE_PROMPT = """\
A research report's title and summary follow. Answer true when it is research
about a company or market (companies, competitors, products, prices, funding,
launches), and false when it is about the user's own life.

Reply with ONLY this JSON: {{"company_research": true}} or {{"company_research": false}}

Title: {title}
Summary:
{summary}"""

# What the model is told when it tries to save a report itself.
REFUSAL = ("Not sent. Waku saves a research report to Waku Memory itself, once "
           "a turn, from your reply. Put the whole report in your reply after its "
           "marker line, and do not save it with this tool.")

# What one earlier report may put in a prompt: title, Summary, key numbers.
DIGEST_CHARS = 1500
TRIMMED = ("[The rest of this report was cut from the context after you read it; "
           "waku_memory_memory_get returns it whole again.]")

_METRICS = re.compile(r"^```waku-metrics\n(.*?)\n```", re.DOTALL | re.MULTILINE)
# A tile that states what a run cost. The model cannot see its own token
# cost, so such a tile is wrong (spec 011: the receipt shows the totals).
_RUN_COST = re.compile(r"\bcost of (this|the) (run|turn|report|research)\b", re.IGNORECASE)

log = logging.getLogger(__name__)


@dataclass
class Report:
    preface: str          # what the reply said before the marker
    body: str             # the report itself, from the marker to the end
    title: str
    summary: list[str] = field(default_factory=list)


def find(reply: str) -> Report | None:
    """The report in `reply`, or None when no line of it is exactly MARKER."""
    lines = (reply or "").splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == MARKER), None)
    if start is None:
        return None
    rest = lines[start + 1:]
    title = next((line[2:].strip() for line in rest if line.startswith("# ")), "")
    summary, inside = [], False
    for line in rest:
        if line.startswith("## "):
            inside = line[3:].strip().lower() == "summary"
        elif inside and line.lstrip().startswith(("- ", "* ")):
            summary.append(line.lstrip()[2:].strip())
    return Report(preface="\n".join(lines[:start]).strip(),
                  body="\n".join(lines[start:]).strip() + "\n",
                  title=title or "Research report", summary=summary)


def holds_report(text: str) -> bool:
    return find(text) is not None


def digest(body: str, limit: int = DIGEST_CHARS) -> str:
    """An earlier report in at most `limit` characters: its title, its
    Summary bullets and its waku-metrics tiles ("key numbers")."""
    report = find(body) or Report(preface="", body=body or "", title="Research report")
    lines = [report.title, *(f"- {b}" for b in report.summary)]
    numbers = []
    for raw in _METRICS.findall(report.body):
        try:
            tiles = json.loads(raw)
        except ValueError:
            continue
        for tile in tiles if isinstance(tiles, list) else []:
            if not isinstance(tile, dict) or _RUN_COST.search(str(tile.get("label", ""))):
                continue
            note = f" ({tile['note']})" if tile.get("note") else ""
            numbers.append(f"{tile.get('label', '')}: {tile.get('value', '')}{note}")
    if numbers:
        lines.append("Numbers: " + "; ".join(numbers))
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def guard_remember(fn: Callable[..., str]) -> Callable[..., str]:
    """Waku Memory's remember tool, as the model sees it, refusing a report.
    Facts and notes go through untouched; only a body holding the marker line
    is refused, so a turn's report has one way into Waku Memory: `save`."""
    def guarded(**kwargs) -> str:
        if holds_report(str(kwargs.get("body") or "")):
            log.info("refused the model's own save of a research report")
            return REFUSAL
        return fn(**kwargs)
    return guarded


def saved_by_model(tool_calls) -> dict | None:
    """The report the model saved itself this turn with Waku Memory's
    remember tool, as {memory_id, scope, body}, or None. A call Waku Memory
    did not take (refused, failed) saved nothing and is not counted."""
    for call in tool_calls or []:
        if not str(call.get("tool", "")).endswith("_memory_remember"):
            continue
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        body = args.get("body")
        if not isinstance(body, str) or not holds_report(body):
            continue
        try:
            memory_id = json.loads(call.get("output") or "")["memory"]["id"]
        except (ValueError, KeyError, TypeError):
            continue
        return {"memory_id": memory_id, "scope": str(args.get("scope") or "global"),
                "body": body}
    return None


def kept_body(reply: str, tool_calls) -> str:
    """The body of the report this turn kept, for consolidation (spec 009 B):
    the one the model saved itself, else the one in the reply, else ""."""
    earlier = saved_by_model(tool_calls)
    if earlier is not None:
        return find(earlier["body"]).body
    report = find(reply)
    return report.body if report is not None else ""


def shrink_read(messages: list[dict]) -> None:
    """Cut each whole report the model already read to its digest.

    Called by the loop before each model call. Every tool result in
    `messages` except the newest (which the model has not read yet) that is a
    memory.get answer holding a report longer than twice DIGEST_CHARS keeps
    the memory's id, kind, scope and date, and its body becomes the digest
    plus a note that memory_get returns it whole again. Without this, three
    earlier reports read in the first iteration rode along on every later
    call of the turn (Sean's rehearsal, 2026-10-05: 328.5k tokens in)."""
    for message in messages[:-1]:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            text = block.get("content")
            if (not isinstance(text, str) or MARKER not in text
                    or len(text) <= 2 * DIGEST_CHARS):
                continue
            short = _shrunk(text)
            if short is not None:
                block["content"] = short


def _shrunk(text: str) -> str | None:
    try:
        answer = json.loads(text)
    except ValueError:
        return None
    if not isinstance(answer, dict):
        return None
    memory = answer.get("memory") if isinstance(answer.get("memory"), dict) else answer
    body = memory.get("body")
    if not isinstance(body, str) or not holds_report(body):
        return None
    kept = {k: memory[k] for k in ("id", "kind", "scope", "created_at") if k in memory}
    kept["body"] = f"{digest(body)}\n\n{TRIMMED}"
    return json.dumps({"memory": kept})


def chat_reply(report: Report) -> str:
    """What the chat keeps: the sentences before the marker, or the Summary's
    bullets when the model wrote none, then where the rest went."""
    lead = report.preface or " ".join(
        b if b.endswith((".", "!", "?")) else b + "." for b in report.summary[:3])
    saved = f"Report saved: {report.title}."
    return f"{lead}\n\n{saved}" if lead else saved


def is_company_research(client: anthropic.Anthropic, small_model: str, report: Report) -> bool:
    """Spec 006's company_research flag, asked of one report. A model may
    answer "true" as a string; anything else, an error included, is personal,
    so a report is never filed into the shared project by mistake."""
    prompt = SCOPE_PROMPT.format(title=report.title,
                                 summary="\n".join(f"- {b}" for b in report.summary))
    try:
        response = client.messages.create(
            model=small_model,
            # room for a reasoning model's thinking block before the JSON,
            # the same reason as the retrieval gate's budget
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        answer = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except Exception:
        return False
    return str(answer.get("company_research")).lower() == "true"


def save(reply: str, remember: Callable | None,
         company_research: Callable[[Report], bool],
         tool_calls=()) -> tuple[str, dict | None]:
    """(the chat reply, the `report` event). The reply comes back unchanged,
    with no event, when it holds no report, when Waku Memory is not connected
    (`remember` is None) and when the send failed. A failure is logged and
    never raised: the turn has already been answered.

    `tool_calls` are the turn's own. When the model already saved a report
    with them, nothing more is sent: the event names that memory, and a
    report in the reply is shortened like any other. One report a turn."""
    report = find(reply)
    earlier = saved_by_model(tool_calls)
    if earlier is not None:
        saved = find(earlier["body"])
        log.info("the model saved this turn's report itself (%s); not sending it again",
                 earlier["memory_id"])
        return (chat_reply(report) if report is not None else reply,
                {"title": saved.title, "memory_id": earlier["memory_id"],
                 "scope": earlier["scope"], "summary": saved.summary})
    if report is None or remember is None:
        return reply, None
    scope = f"project:{COMPANY_PROJECT}" if company_research(report) else "global"
    try:
        memory_id = remember(report.body, scope, kind=KIND)
    except Exception as exc:
        log.warning("Waku Memory did not take the report %r (%s); "
                    "it stays in the reply", report.title, exc)
        return reply, None
    return chat_reply(report), {"title": report.title, "memory_id": memory_id,
                                "scope": scope, "summary": report.summary}
