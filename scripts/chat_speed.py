"""Time ten simple questions against a person's hosted Waku Agent (spec 039 A1).

A person who asks the phone one simple question waits about 15 seconds for
the first word, and the question sends about 138,000 input tokens. This
script measures both, so a change to the hosted agent is judged by numbers
rather than by feel. It sends each of QUESTIONS to `POST /v1/chat`, the same
front door the phone and waku.one use, and writes one CSV row per question:

    first_event_s   seconds from send to the first stream event
    first_text_s    seconds from send to the first word of the answer
    done_s          seconds from send to the `done` event
    model_calls     every model call the turn made, side calls included
    loop_calls      the loop's own calls (kind "llm" or "final")
    input_tokens, cache_read_tokens, cache_write_tokens, output_tokens
                    summed over the turn's calls
    calls           each call as kind:in/cache_read/cache_write/out
    turn_id, model_usd, usd_is_estimate, credits
                    from the receipt the `done` event carries

The per-call tokens come from the `llm` events in the stream. A container
built before spec 039 B reports no cache fields, and the script counts them
as zero. The dollars and credits come from the receipt, which the container
fills from the metering proxy's `GET /v1/turns/<turn_id>/charges`. That route
answers only a container's own platform token, so this script cannot call it
and reads the same numbers from the receipt instead.

Run it three times per change: warm, warm again, and cold after 16 idle
minutes. The token is the person's Supabase access token, read from the
environment and never printed, logged or written to the CSV:

    WAKU_CHAT_TOKEN=...  python scripts/chat_speed.py --label warm --out warm.csv
    WAKU_CHAT_URL=https://agent.waku.one  (the default)

`--parts "<message>"` makes no network call. It builds the hosted agent's
built-in tools and system prompt locally (WAKU_HOSTED=1, a temporary home)
and prints each part's size as characters / 4, labelled an estimate. MCP
tools (Waku Memory, Notion, Linear, GitHub, treg) are not connected locally,
so they are not in that count.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path

DEFAULT_URL = "https://agent.waku.one"
TOKEN_ENV = "WAKU_CHAT_TOKEN"
URL_ENV = "WAKU_CHAT_URL"
CHAT_PATH = "/v1/chat"
TIMEOUT_SECONDS = 180.0

# Ten simple questions: the two spec 039 names, then questions about the
# person's own memory and calendar of the kind the phone issues list, and one
# greeting.
QUESTIONS = (
    "What did I decide about pricing last week?",
    "Who is Emily?",
    "Hi Waku, how are you?",
    "What is on my calendar tomorrow?",
    "When is my next meeting with Ian?",
    "What did I say I would do this week?",
    "What do you know about me?",
    "Which company did I say I was talking to about a pilot?",
    "When did I last talk about the launch video?",
    "What was the last thing I asked you to remember?",
)

CSV_FIELDS = ("label", "n", "question", "first_event_s", "first_text_s", "done_s",
              "model_calls", "loop_calls", "input_tokens", "cache_read_tokens",
              "cache_write_tokens", "output_tokens", "calls", "turn_id", "model_usd",
              "usd_is_estimate", "credits", "error")

# The loop's own calls arrive as kind "llm", and its one tools-off call after
# the step limit as "final". A side call (gate, consolidation, triage, quick,
# report) arrives under its own kind, because the container writes each frame
# as {"kind": kind, **event} and a side call's event names its kind.
LOOP_KINDS = ("llm", "final")


def _tokens(usage: dict, name: str) -> int:
    value = usage.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _seconds(at: float | None, start: float) -> float | None:
    return None if at is None else round(at - start, 3)


def _row(frames: list[dict], times: dict, start: float) -> dict:
    """One CSV row from the frames one turn streamed."""
    calls = []
    for frame in frames:
        usage = frame.get("usage")
        if not isinstance(usage, dict):
            continue
        kind = "loop" if frame.get("kind") == "llm" else str(frame.get("kind") or "loop")
        calls.append((kind, _tokens(usage, "in"), _tokens(usage, "cache_read"),
                      _tokens(usage, "cache_write"), _tokens(usage, "out")))
    done = next((f for f in frames if f.get("kind") == "done"), {})
    receipt = done.get("receipt") if isinstance(done.get("receipt"), dict) else {}
    model = receipt.get("model") if isinstance(receipt.get("model"), dict) else {}
    error = done.get("error") or done.get("notice") or ""
    if not done and not error:
        error = "the stream ended without a done event"
    return {
        "first_event_s": _seconds(times.get("event"), start),
        "first_text_s": _seconds(times.get("text"), start),
        "done_s": _seconds(times.get("done"), start),
        "model_calls": len(calls),
        "loop_calls": sum(1 for c in calls if c[0] in ("loop", "final")),
        "input_tokens": sum(c[1] for c in calls),
        "cache_read_tokens": sum(c[2] for c in calls),
        "cache_write_tokens": sum(c[3] for c in calls),
        "output_tokens": sum(c[4] for c in calls),
        "calls": "; ".join(f"{k}:{i}/{r}/{w}/{o}" for k, i, r, w, o in calls),
        "turn_id": receipt.get("turn_id") or "",
        "model_usd": model.get("usd"),
        "usd_is_estimate": model.get("estimate"),
        "credits": receipt.get("credits"),
        "error": str(error),
    }


def ask(base_url: str, token: str, message: str, *,
        timeout: float = TIMEOUT_SECONDS, clock=time.monotonic) -> dict:
    """Send one message to /v1/chat and time its stream. Never raises for an
    answer the server gave: an HTTP error or a refusal is the row's error."""
    request = urllib.request.Request(
        base_url.rstrip("/") + CHAT_PATH, method="POST",
        data=json.dumps({"message": message}).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": "text/event-stream"})
    frames: list[dict] = []
    times: dict[str, float] = {}
    start = clock()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - the person's own front door
            while True:
                line = response.readline()
                if not line:
                    break
                line = line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    frame = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if not isinstance(frame, dict):
                    continue
                at = clock()
                times.setdefault("event", at)
                if frame.get("kind") == "text" and frame.get("delta"):
                    times.setdefault("text", at)
                frames.append(frame)
                if frame.get("kind") == "done":
                    times["done"] = at
                    break
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            said = json.loads(body).get("error") or body
        except (ValueError, AttributeError):
            said = body
        row = _row([], {}, start)
        row["error"] = f"HTTP {exc.code}: {str(said)[:200]}"
        return row
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        row = _row(frames, times, start)
        row["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return row
    return _row(frames, times, start)


def local_parts(message: str, home: Path | None = None) -> dict:
    """The hosted agent's request for `message`, built locally and sized by
    characters / 4: built-in tool schemas, system prompt, history, message.

    It uses a temporary home with the hosted SOUL.md, so nothing of the
    person's own machine is read, and leaves the platform address unset, so
    no tool asks the platform anything."""
    previous = os.environ.get("WAKU_HOSTED")
    os.environ["WAKU_HOSTED"] = "1"
    try:
        with tempfile.TemporaryDirectory() as scratch:
            where = Path(home or scratch)
            soul = Path(__file__).resolve().parents[1] / "hosted" / "templates" / "SOUL.md"
            (where / "SOUL.md").write_text(soul.read_text(encoding="utf-8"), encoding="utf-8")
            from waku.config import Settings
            from waku.db import connect
            from waku.runtime.session import Session
            from waku.tools import build_registry

            settings = Settings(home=where, apple_calendar=False, google_calendar=False,
                                apple_tools=False)
            conn = connect(where)
            try:
                registry = build_registry(conn, settings)
                schemas = registry.schemas()
                system = Session(settings).build_system(message)
            finally:
                conn.close()
    finally:
        if previous is None:
            os.environ.pop("WAKU_HOSTED", None)
        else:
            os.environ["WAKU_HOSTED"] = previous
    return {"estimate": True, "tool_count": len(schemas),
            "tools": len(json.dumps(schemas)) // 4, "system": len(system) // 4,
            "history": 0, "message": len(message) // 4}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Time ten simple questions against a hosted Waku Agent (spec 039 A1). "
                    f"The token is read from {TOKEN_ENV}, never from the command line.")
    parser.add_argument("--label", help="warm, warm2 or cold: written on every row")
    parser.add_argument("--out", help="the CSV to write (default chat_speed-<label>.csv)")
    parser.add_argument("--limit", type=int, default=len(QUESTIONS),
                        help="send only the first N questions")
    parser.add_argument("--parts", metavar="MESSAGE",
                        help="print the local per-part estimate for MESSAGE and exit; no network")
    args = parser.parse_args(argv)
    if not args.parts and not args.label:
        parser.error("--label is required")
    return args


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    if args.parts:
        parts = local_parts(args.parts)
        print("Estimate: characters / 4, built-in tools only (MCP tools are not connected locally).")
        for name in ("tools", "system", "history", "message"):
            print(f"  {name:8} {parts[name]:>7} tokens")
        print(f"  ({parts['tool_count']} built-in tool schemas)")
        return 0
    token = env.get(TOKEN_ENV, "").strip()
    if not token:
        print(f"Set {TOKEN_ENV} to your Supabase access token first. "
              "The script reads it from the environment only.", file=sys.stderr)
        return 2
    base_url = env.get(URL_ENV, "").strip() or DEFAULT_URL
    out = Path(args.out or f"chat_speed-{args.label}.csv")
    rows = []
    for n, question in enumerate(QUESTIONS[:max(args.limit, 0)], start=1):
        row = {"label": args.label, "n": n, "question": question,
               **ask(base_url, token, question)}
        rows.append(row)
        print(f"{n:>2}  event {row['first_event_s']}s  word {row['first_text_s']}s  "
              f"done {row['done_s']}s  calls {row['model_calls']}  in {row['input_tokens']}  "
              f"credits {row['credits']}  {row['error']}".rstrip())
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
