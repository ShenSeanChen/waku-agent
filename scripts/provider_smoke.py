"""Send one real tool turn to every provider that is set up, and say which answered.

Why this exists: every provider check in evals/deterministic/ runs against a
fake model, so a model that has stopped answering, or that cannot call tools,
reached a user before it reached us (issue #137, and on 2026-11-22 Gemini's
streamed tool turns failed on main while every offline test passed). This
script asks each real model once.

It needs real keys and costs a few cents, so it runs by hand and before a
release, never in CI:

    uv run python scripts/provider_smoke.py               # every provider that is set up
    uv run python scripts/provider_smoke.py local gemini  # only these
    uv run python scripts/provider_smoke.py --fast        # the fast model instead of the default
    uv run python scripts/provider_smoke.py local --model qwen3:14b   # one model by name

A provider counts as set up when its key is in the environment, or, for a row
that needs no key (`local`), when its server answers. The script prints a
provider's name, its model, PASS or FAIL and the reason. It never prints a key.
It exits 1 when any provider fails.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.request
from types import SimpleNamespace

from waku.config import load_settings
from waku.loop.agent import run_loop
from waku.loop.models import PROVIDERS, get_client

PROMPT = "Remember that Alex prefers morning meetings."
TOOL = {
    "name": "save_note",
    "description": "Save a note the user asks you to remember.",
    "input_schema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"]},
}


def _local_server_answers(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/models", timeout=3):  # noqa: S310 - the person's own server
            return True
    except Exception:
        return False


def _ready(name: str) -> tuple[bool, str]:
    provider = PROVIDERS[name]
    if not provider.is_visible():
        return False, "hidden on this machine"
    if os.getenv(provider.key_env, "").strip():
        return True, ""
    if provider.key_optional:
        base = provider.configured_base_url() or ""
        if _local_server_answers(base):
            return True, ""
        return False, f"no server answering at {base}"
    return False, f"{provider.key_env} is not set"


def _one_turn(name: str, fast: bool, model: str = "") -> tuple[str, bool, str, float]:
    provider = PROVIDERS[name]
    settings = load_settings()
    settings.provider = name
    settings.api_key = ""
    settings.base_url = None
    pair = provider.default_pair()
    settings.model = model or (pair[-1] if fast else (pair[0] if pair else ""))
    settings.small_model = ""
    client = get_client(settings)
    calls: list = []
    tools = SimpleNamespace(schemas=lambda: [TOOL],
                            execute=lambda *a, **kw: calls.append(a) or "Saved.")
    started = time.monotonic()
    try:
        result = run_loop(client, settings.model, "You are a helpful assistant.",
                          [{"role": "user", "content": PROMPT}], tools, stream=False)
    except Exception as exc:  # noqa: BLE001 - the reason is the output
        return settings.model, False, f"{type(exc).__name__}: {str(exc)[:160]}", time.monotonic() - started
    seconds = time.monotonic() - started
    if not calls:
        return settings.model, False, f"answered without calling a tool: {result.reply[:80]!r}", seconds
    return settings.model, True, f"called save_note, replied {result.reply[:60]!r}", seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("providers", nargs="*", help="provider names; default is every one that is set up")
    parser.add_argument("--fast", action="store_true", help="use each provider's fast model")
    parser.add_argument("--model", default="", help="one model id to try, for a single provider")
    args = parser.parse_args(argv)

    if args.model and len(args.providers) != 1:
        print("--model needs exactly one provider name.")
        return 2
    unknown = [n for n in args.providers if n not in PROVIDERS]
    if unknown:
        print(f"Unknown provider: {', '.join(unknown)}. Known: {', '.join(sorted(PROVIDERS))}")
        return 2

    failed = 0
    for name in args.providers or sorted(PROVIDERS):
        ready, why = _ready(name)
        if not ready:
            if args.providers:
                print(f"SKIP  {name:<14} {why}")
            continue
        model, ok, detail, seconds = _one_turn(name, args.fast, args.model)
        failed += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {name:<14} {model:<32} {seconds:5.1f}s  {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
