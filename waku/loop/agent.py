"""THE LOOP — observe → reason → act → repeat. This file is the whole trick.

Every agent framework is ultimately this while-loop with more indirection:

    while not done:
        response = llm(messages, tools)          # reason
        if response asks for tools:
            results = run(tool_calls)            # act
            messages += results                  # observe
        else:
            done                                 # reply to the human

End-loop guardrails (the orange box's exit conditions):
  1. the model stops asking for tools  → natural end of turn
  2. max_iterations reached            → hard stop, never spin forever: one
     last call with tools off answers from what the turn already gathered
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anthropic

from waku.tools.registry import ToolRegistry

# Observers let the gateway show tool calls live and let ops/tracing record
# them — without either being wired into the loop's logic.
LoopEvent = dict[str, Any]
Observer = Callable[[str, LoopEvent], None]

log = logging.getLogger(__name__)

# Guardrail 2's words. LIMIT_NOTE goes to the model for its one tools-off
# call; LIMIT_REPLY is what the person reads if that call fails too.
LIMIT_NOTE = ("You have reached the step limit for this turn and cannot call any more "
              "tools. Answer now from what you already gathered; say plainly what is "
              "missing or what failed.")
LIMIT_REPLY = "(I hit my iteration limit before finishing — try breaking the request into smaller steps.)"


# The hosted proxy's code for a person whose Waku credits ran out (spec 042).
# Its message is a sentence written for that person, with the link to pay.
CREDITS_USED_UP = "credits_used_up"


def credits_notice(exc: BaseException) -> str | None:
    """The sentence to show when the provider refused the call because the
    person is out of Waku credits, or None for any other failure.

    The SDK keeps the refusal's JSON on `exc.body`. The code is read rather
    than the English, so a copy edit on the platform never breaks this."""
    body = getattr(exc, "body", None)
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict) or error.get("code") != CREDITS_USED_UP:
        return None
    message = error.get("message")
    return message.strip() if isinstance(message, str) and message.strip() else None


def error_text(exc: BaseException) -> str:
    """What the user reads when a call fails.

    A provider that refused the call already wrote a sentence for a human;
    show that one. Anything else keeps today's {type}: {message}, which is
    the right amount of detail for a bug rather than a decision.

    It lives here, beside the 4xx rule above, because a refusal reaches a
    reader by two routes — the gateway's "done" event and a graph node's
    entry in a run's `errors` map — and two copies of this would drift the
    way the dashboard's two chat implementations once did.
    """
    notice = credits_notice(exc)
    if notice:
        return notice
    message = getattr(exc, "message", "")
    if message and getattr(exc, "status_code", 0):
        return str(message)
    return f"{type(exc).__name__}: {exc}"


# Spec 039 B3: Anthropic's five-minute prompt-cache breakpoint.
CACHE_BREAKPOINT = {"type": "ephemeral"}


def cached_system(system: str, prefix: str) -> str | list[dict]:
    """`system` as Anthropic text blocks with one cache breakpoint at the end
    of `prefix`, or `system` unchanged when there is no prefix or the prompt
    does not start with it. Anthropic renders the tools before the system
    prompt, so the breakpoint caches the tool schemas and the prefix both."""
    if not prefix or not system.startswith(prefix):
        return system
    blocks = [{"type": "text", "text": prefix, "cache_control": dict(CACHE_BREAKPOINT)}]
    if rest := system[len(prefix):]:
        blocks.append({"type": "text", "text": rest})
    return blocks


def _usage(response) -> dict:
    """A call's tokens for the `llm` event: input, output, and the prompt-cache
    reads and writes, which are zero for a provider that reports none."""
    usage = response.usage

    def count(name: str) -> int:
        value = getattr(usage, name, 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    return {"in": count("input_tokens"), "out": count("output_tokens"),
            "cache_read": count("cache_read_input_tokens"),
            "cache_write": count("cache_creation_input_tokens")}


@dataclass
class LoopResult:
    reply: str
    tool_calls: list[LoopEvent] = field(default_factory=list)
    iterations: int = 0
    # Spec 009 A: the Waku Memory searches the harness ran before the loop, on
    # a research turn, and the memories they found (the turn's Used list).
    # Kept apart from tool_calls on purpose: tool_calls are folded into the
    # chat log, which consolidation reads, and memories already kept must not
    # be proposed as new facts again.
    read_first: list[LoopEvent] = field(default_factory=list)
    used: list[dict] = field(default_factory=list)
    # The memory the turn read before the loop, as text: what the retrieval
    # gate found plus the research block above. Consolidation drops a fact
    # that only repeats it (and what the model's own memory reads returned).
    recalled: str = ""
    # Spec 011: what the turn did and cost (waku/ops/receipt.py), set by
    # Waku.respond once the turn is over. None for a bare run_loop call.
    receipt: dict | None = None
    # True when the turn hit max_iterations and the reply came from the one
    # tools-off call after it (guardrail 2), not from a natural end.
    limit_reached: bool = False
    # Spec 021: the lines a plugin's hooks asked to show the person
    # (systemMessage). The dashboard draws them under the reply.
    notices: list[str] = field(default_factory=list)
    # Spec 038: the lines about connectors left out for a sign-in. They are in
    # `notices` too; this copy is what a text gateway adds to its reply,
    # since a phone has no place under the reply to draw them.
    sign_in_notices: list[str] = field(default_factory=list)
    # Spec 042: "ok", "low" or "empty", what the hosted proxy's charges
    # answer said about the person's Waku credits after this turn. None
    # off the hosted platform, or when the proxy did not say.
    credits_state: str | None = None


def text_with_sign_in(result) -> str:
    """A turn's reply as a text gateway sends it: the reply, then any line
    about a connector left out for its sign-in (spec 038)."""
    reply = getattr(result, "reply", "") or ""
    lines = getattr(result, "sign_in_notices", None) or []
    return "\n\n".join([reply, *lines]) if lines else reply


def run_loop(
    client: anthropic.Anthropic,
    model: str,
    system: str,
    messages: list[dict],
    tools: ToolRegistry,
    max_iterations: int = 10,
    max_tokens: int = 2048,
    observer: Observer | None = None,
    stream: bool = False,
    trim: Callable[[list[dict]], None] | None = None,
    before_tool: Callable[[str, dict], tuple[str | None, str]] | None = None,
    cache_prefix: str = "",
) -> LoopResult:
    """Run one agent turn. `messages` is mutated in place — after the call it
    contains the full working memory of the turn (assistant thoughts, tool
    calls, tool results), which is exactly what gets traced.

    stream=True emits the assistant's text as it's generated (notify("text",
    {"delta": ...})) so a gateway can show it appear token by token — used by
    the dashboard. Falls back to a single call for clients without streaming.

    `trim`, when given, may shorten tool results the model has already read
    before each later call, so one large result is not re-sent on every
    iteration after it. app.py chains reports.shrink_read, which cuts a whole
    earlier research report to its digest, and trim.shrink_seen, which cuts
    any other long result to its opening; None leaves `messages` as they are.

    `before_tool`, when given, is asked before each tool call (spec 021's
    PreToolUse). It returns (a reason to refuse, or None; context to add).
    A refused call does not run, and the model reads the reason as its
    result. The loop does not know where the answer came from.

    `cache_prefix`, when given and the system prompt starts with it, is sent
    as its own text block with a prompt-cache breakpoint (spec 039 B3), so
    every call of the turn after the first reads the tools and that prefix
    from the cache. Only a provider that takes Anthropic's `cache_control`
    is given one."""
    notify = observer or (lambda kind, ev: None)
    prompt = cached_system(system, cache_prefix)
    result = LoopResult(reply="")
    can_stream = stream and hasattr(client.messages, "stream")

    for iteration in range(1, max_iterations + 1):
        result.iterations = iteration
        if trim is not None and iteration > 1:
            trim(messages)

        # ---- reason: one LLM call with the current working memory
        response = None
        if can_stream:
            try:
                with client.messages.stream(
                    model=model, system=prompt, messages=messages,
                    tools=tools.schemas(), max_tokens=max_tokens,
                ) as s:
                    for delta in s.text_stream:
                        notify("text", {"delta": delta})
                    response = s.get_final_message()
            except Exception as exc:
                # A 4xx is a decision, not a transport fault: the provider
                # looked at the request and refused it. Retrying without
                # streaming asks the same question and gets the same answer,
                # which doubles what a metered tenant spends on being told no.
                if 400 <= getattr(exc, "status_code", 0) < 500:
                    raise
                response = None  # any other streaming hiccup → fall back to one call
        if response is None:
            response = client.messages.create(
                model=model,
                system=prompt,
                messages=messages,
                tools=tools.schemas(),
                max_tokens=max_tokens,
            )
        notify("llm", {"iteration": iteration, "stop_reason": response.stop_reason,
                       "usage": _usage(response)})

        # the assistant's turn (text and/or tool requests) joins working memory
        messages.append({"role": "assistant", "content": response.content})

        tool_uses = [b for b in response.content if b.type == "tool_use"]

        # ---- guardrail 1: no tool calls → the model is talking to the human
        if not tool_uses:
            result.reply = "".join(b.text for b in response.content if b.type == "text")
            return result

        # ---- act: execute each requested tool; observe: feed results back
        tool_results = []
        for call in tool_uses:
            # Spec 039 E1: say a tool has started before it runs, so a client
            # can show "Searching your memories" instead of an empty screen.
            # The `tool` event below follows with the same call_id.
            notify("tool_start", {"tool": call.name, "call_id": call.id})
            started = time.perf_counter()
            deny, extra = before_tool(call.name, call.input) if before_tool else (None, "")
            if deny is not None:
                output = f"A plugin hook stopped this call before it ran: {deny}"
            else:
                output = tools.execute(call.name, call.input, notify=notify)
                if extra:
                    output = f"{output}\n\n{extra}"
            event = {"tool": call.name, "args": call.input, "output": output, "call_id": call.id,
                     # spec 012: how long the call took, for the trace
                     "duration_ms": int((time.perf_counter() - started) * 1000)}
            if deny is not None:
                event["denied"] = True
            result.tool_calls.append(event)
            notify("tool", event)
            tool_results.append(
                {"type": "tool_result", "tool_use_id": call.id, "content": output}
            )
        messages.append({"role": "user", "content": tool_results})

    # ---- guardrail 2: ran out of iterations. Everything gathered so far is
    # in `messages`, so ask once more with tools off for an answer from it,
    # rather than throwing it away. If that call fails, say so the old way.
    result.limit_reached = True
    result.reply = _final_answer(client, model, prompt, messages, tools, max_tokens,
                                 notify, trim, max_iterations + 1) or LIMIT_REPLY
    return result


def _final_answer(client, model: str, system: str | list[dict], messages: list[dict],
                  tools: ToolRegistry,
                  max_tokens: int, notify: Observer, trim, iteration: int) -> str:
    """The one tools-off call after max_iterations: its text, or "" when it
    failed or wrote none. The tool definitions are still sent, because
    Anthropic refuses a history with tool_use blocks and no tools; tool_choice
    "none" is what stops the model calling one."""
    if trim is not None:
        trim(messages)
    note = f"\n\n{LIMIT_NOTE}"
    final_system = ([*system, {"type": "text", "text": note}] if isinstance(system, list)
                    else system + note)
    try:
        response = client.messages.create(
            model=model, system=final_system, messages=messages,
            tools=tools.schemas(), tool_choice={"type": "none"}, max_tokens=max_tokens)
    except Exception as exc:
        log.warning("the final answer after the iteration limit failed: %s", error_text(exc))
        return ""
    # kind "final", not "loop": the waterfall keeps it in the last loop's row
    # and the receipt counts it apart; final_answer is the flag to look for
    notify("llm", {"iteration": iteration, "kind": "final", "final_answer": True,
                   "stop_reason": response.stop_reason,
                   "usage": _usage(response)})
    text = [b for b in response.content if b.type == "text"]
    if text:
        messages.append({"role": "assistant", "content": text})
    return "".join(b.text for b in text)
