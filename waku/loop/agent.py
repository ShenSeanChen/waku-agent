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
    message = getattr(exc, "message", "")
    if message and getattr(exc, "status_code", 0):
        return str(message)
    return f"{type(exc).__name__}: {exc}"


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
    any other long result to its opening; None leaves `messages` as they are."""
    notify = observer or (lambda kind, ev: None)
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
                    model=model, system=system, messages=messages,
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
                system=system,
                messages=messages,
                tools=tools.schemas(),
                max_tokens=max_tokens,
            )
        notify("llm", {"iteration": iteration, "stop_reason": response.stop_reason,
                       "usage": {"in": response.usage.input_tokens, "out": response.usage.output_tokens}})

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
            started = time.perf_counter()
            output = tools.execute(call.name, call.input, notify=notify)
            event = {"tool": call.name, "args": call.input, "output": output, "call_id": call.id,
                     # spec 012: how long the call took, for the trace
                     "duration_ms": int((time.perf_counter() - started) * 1000)}
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
    result.reply = _final_answer(client, model, system, messages, tools, max_tokens,
                                 notify, trim, max_iterations + 1) or LIMIT_REPLY
    return result


def _final_answer(client, model: str, system: str, messages: list[dict], tools: ToolRegistry,
                  max_tokens: int, notify: Observer, trim, iteration: int) -> str:
    """The one tools-off call after max_iterations: its text, or "" when it
    failed or wrote none. The tool definitions are still sent, because
    Anthropic refuses a history with tool_use blocks and no tools; tool_choice
    "none" is what stops the model calling one."""
    if trim is not None:
        trim(messages)
    try:
        response = client.messages.create(
            model=model, system=f"{system}\n\n{LIMIT_NOTE}", messages=messages,
            tools=tools.schemas(), tool_choice={"type": "none"}, max_tokens=max_tokens)
    except Exception as exc:
        log.warning("the final answer after the iteration limit failed: %s", error_text(exc))
        return ""
    # kind "final", not "loop": the waterfall keeps it in the last loop's row
    # and the receipt counts it apart; final_answer is the flag to look for
    notify("llm", {"iteration": iteration, "kind": "final", "final_answer": True,
                   "stop_reason": response.stop_reason,
                   "usage": {"in": response.usage.input_tokens, "out": response.usage.output_tokens}})
    text = [b for b in response.content if b.type == "text"]
    if text:
        messages.append({"role": "assistant", "content": text})
    return "".join(b.text for b in text)
