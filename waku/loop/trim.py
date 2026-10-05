"""Old tool results, cut once the model has read them.

The loop re-sends the whole conversation on every call, so a large tool
result rides along on every iteration after the one that produced it. On
2026-10-05 one treg research turn made 8 calls in 8 iterations (Reddit and X
searches, YouTube comments) and sent 626.5k tokens in for 8.4k out: the model
cost $1.334 and treg $0.007. Most of those tokens were search and comment JSON
the model had already read.

`shrink_seen` is a `trim` step for run_loop (waku/loop/agent.py). Before each
call after the first, every tool result except the newest message's is one
the model has already read; a result longer than LONG_CHARS keeps its first
KEEP_CHARS characters and a note saying how long it was. The newest results
are never touched: the model has not read them yet. The trace keeps every
output whole (waku/ops/tracing.py), so nothing is lost to the person.

Results older than the latest iteration are the only ones cut, so a provider
that caches a prompt's prefix loses at most the part after the result cut in
this call, and each result is cut once: a cut result is shorter than
LONG_CHARS and is never cut again.
"""

from __future__ import annotations

from collections.abc import Callable

# A result this long or shorter is left whole: a calendar answer or a memory
# fact costs less than the note that would replace it.
LONG_CHARS = 4000
# What a cut result keeps: its opening, which for search JSON holds the first
# few hits and their fields.
KEEP_CHARS = 2000
NOTE = ("\n\n[Cut from {total:,} characters after you read it. The whole output is in "
        "this turn's trace. Results you have read are cut before each later call, so "
        "write down what you need from a result before you call the next tool.]")

Trim = Callable[[list[dict]], None]


def shrink_seen(messages: list[dict]) -> None:
    """Cut each long tool result the model has already read, in place."""
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
            if isinstance(text, str) and len(text) > LONG_CHARS:
                block["content"] = text[:KEEP_CHARS] + NOTE.format(total=len(text))


def chain(*steps: Trim) -> Trim:
    """One `trim` that runs `steps` in order. app.py runs reports.shrink_read
    first, so an earlier report becomes its digest rather than its first
    KEEP_CHARS characters."""
    def run(messages: list[dict]) -> None:
        for step in steps:
            step(messages)
    return run
