"""Pricing — what a call cost, in dollars a human can feel.

The loop writes a permanent ledger (`.waku/usage.jsonl`): one line per API call
with provider, model, and token counts. It deliberately does NOT store a price.
Prices change; tokens don't. So cost is derived HERE, at read time, from the
current tables — which means fixing a wrong rate silently corrects every past
race and every historical spend chart.

Three tables, checked in this order by `price_for()`:

  _price_cache    exact per-model rates learned from a live catalog fetch
                  (OpenRouter reports them) — see catalog.list_models()
  MODEL_PRICING   hand-maintained rates for endpoints with no listable catalog
  PRICING         a provider-level fallback, deliberately rough, labelled "est"

`MODEL_CUTOFF` is the same idea for knowledge cutoffs: the arena discloses when
each brain's world knowledge ends, so a 2025 model denying that 2026 models
exist reads as stale data, not stupidity.

Imported by the arena (per-race cost), the dashboard (the spend chart), and
`scripts/shootout.py`.
"""

from __future__ import annotations

import json

# Rough $/million tokens (in, out) for a dollar ESTIMATE — the number humans
# actually feel. Keyed by provider; deliberately approximate and labelled "est".
PRICING = {
    "anthropic": (3.0, 15.0), "openai": (2.5, 15.0), "gemini": (0.3, 2.5),
    "deepseek": (0.435, 0.87), "minimax": (0.30, 1.20), "kimi": (0.6, 2.5), "glm": (0.6, 2.2),
    "xai": (3.0, 15.0),   # Grok — rough est; keyed users get exact from the catalog
    "opencode_zen": (0.435, 0.87),   # rough est (matching deepseek — same underlying model)
    "opencode_go": (0.435, 0.87),    # rough est
    # openrouter fallback for paid models when the live catalog is unreachable
    # (rough mid-catalog guess). ":free" ids and catalog-priced models never
    # hit this: see price_for().
    "openrouter": (1.0, 3.0),
}

# model id -> exact ($/M in, $/M out), filled from the live catalog fetch in
# list_models(). OpenRouter reports per-model pricing, so cost estimates can
# be exact per call instead of one number per provider.
_price_cache: dict[str, tuple[float, float]] = {}


# Known per-model prices ($/M in, out) for endpoints with no listable catalog
# (the anthropic wire has no /models), checked before the provider-level
# fallback. Within a provider, models diverge a LOT — fable-5 is ~2x opus,
# gemini-flash undercuts gemini-pro — so pricing per *model* is the only honest
# way; a provider-level guess made fable-5 look cheaper than opus. Rates are
# standard short-context list prices (cache/batch discounts not modelled),
# fact-checked Jul 2026 against each vendor's pricing page. See docs/benchmarks.md.
MODEL_PRICING = {
    # Anthropic — platform.claude.com/docs/.../pricing
    "claude-opus-4-8": (5.0, 25.0),
    "claude-fable-5": (10.0, 50.0),            # Mythos-class flagship, ~2x opus
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    # OpenAI — openai.com pricing (Sol = flagship; chat-latest = non-reasoning)
    "gpt-5.6-sol": (5.0, 30.0),
    "gpt-5.3-chat-latest": (1.75, 14.0),
    # Google Gemini — ai.google.dev pricing (standard <200k tier)
    "gemini-3.1-pro-preview": (2.0, 12.0),
    "gemini-3.5-flash": (1.5, 9.0),
    # Moonshot Kimi — platform.kimi.ai (highspeed = 2x the standard k2.7 rate)
    "kimi-k3": (3.0, 15.0),
    "kimi-k2.7-code-highspeed": (1.9, 8.0),
    "kimi-k2.7": (0.95, 4.0),
    # xAI Grok — docs.x.ai/developers/pricing
    "grok-4.5": (2.0, 6.0),
    "grok-4.3": (1.25, 2.5),
    # OpenCode / deepseek — the default model for opencode_go
    "deepseek-v4-flash": (0.15, 0.30),
    # free tier on opencode_zen
    "deepseek-v4-flash-free": (0.0, 0.0),
}


# Knowledge cutoff (YYYY-MM) per model — the arena discloses when each brain's
# world knowledge ends, so stale knowledge isn't misread as low capability
# (gemini-3.1-pro will confidently deny that 2026 models exist: its cutoff is
# 2025-01, a year before them). Values are each vendor's published knowledge
# cutoff (for Anthropic, the "reliable knowledge cutoff"; training data runs
# later). None = the vendor hasn't published one. Fact-checked Jul 2026 against
# vendor model cards/docs. Every MODEL_PRICING id must have an entry here —
# enforced by evals/deterministic/test_providers.py.
MODEL_CUTOFF = {
    # Anthropic — support.claude.com "How up-to-date is Claude's training data?"
    "claude-opus-4-8": "2026-01",
    "claude-fable-5": "2026-01",
    "claude-sonnet-5": "2026-01",
    "claude-haiku-4-5-20251001": "2025-02",   # trained on data through 2025-07
    # OpenAI — developers.openai.com model pages
    "gpt-5.6-sol": "2026-02",
    "gpt-5.3-chat-latest": "2025-08",
    # Google Gemini — deepmind.google model cards / ai.google.dev
    "gemini-3.1-pro-preview": "2025-01",
    "gemini-3.5-flash": "2025-01",
    # Moonshot Kimi — K3 reported "early 2026"; K2.7 cutoffs unpublished
    "kimi-k3": "2026-01",
    "kimi-k2.7-code-highspeed": None,
    "kimi-k2.7": None,
    # xAI Grok — docs.x.ai model list
    "grok-4.5": "2026-02",
    "grok-4.3": "2025-12",
    # OpenCode / deepseek
    "deepseek-v4-flash": "2026-04",
    "deepseek-v4-flash-free": "2026-04",
}


def remember_price(model: str, price_in: float, price_out: float) -> None:
    """Record an EXACT per-model rate learned from a live catalog fetch, so
    `price_for` stops guessing for this model. Called by catalog.list_models()
    when the endpoint reports pricing (OpenRouter does). Process-lifetime only —
    the ledger stores tokens, so a restart just falls back to the tables above."""
    _price_cache[model] = (price_in, price_out)


def cutoff_for(model: str) -> str | None:
    """Knowledge-cutoff date ('YYYY-MM') for a model id, or None when the
    vendor hasn't published one (the UI shows a dash rather than a guess)."""
    return MODEL_CUTOFF.get(model)


def price_for(provider: str, model: str) -> tuple[float, float]:
    """$/M tokens (in, out) for one call: the catalog's per-model price when
    known, $0 for ":free" ids, a known MODEL_PRICING id, else the provider-level
    PRICING estimate."""
    if model in _price_cache:
        return _price_cache[model]
    if model.endswith(":free"):
        return (0.0, 0.0)
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    return PRICING.get(provider, (3.0, 15.0))


def usage_summary(home) -> dict:
    """Read the PERMANENT spend ledger (usage.jsonl) → all-time tokens + dollar
    cost, plus per-day and per-provider breakdowns. Cost is derived from tokens
    with PRICING (approximate, labelled 'est'). This survives demo resets, so the
    number is the real running total — trustworthy, not a per-session guess."""
    recs = []
    path = home / "usage.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    def cost(r) -> float:
        # the ledger stores tokens + provider/model, so old rows reprice too
        pin, pout = price_for(r.get("provider", ""), r.get("model", ""))
        return r.get("in", 0) / 1e6 * pin + r.get("out", 0) / 1e6 * pout

    def add(bucket, key, extra):
        b = bucket.setdefault(key, {**extra, "calls": 0, "in": 0, "out": 0, "cost": 0.0})
        b["calls"] += 1
        b["in"] += r.get("in", 0)
        b["out"] += r.get("out", 0)
        b["cost"] += cost(r)

    by_day, by_provider = {}, {}
    for r in recs:
        day = (r.get("ts") or "")[:10]
        add(by_day, day, {"date": day})
        add(by_provider, r.get("provider", "?"), {"provider": r.get("provider", "?")})

    return {
        "calls": len(recs),
        "total_in": sum(r.get("in", 0) for r in recs),
        "total_out": sum(r.get("out", 0) for r in recs),
        "total_cost": round(sum(cost(r) for r in recs), 4),
        "by_day": sorted(by_day.values(), key=lambda x: x["date"], reverse=True)[:30],
        "by_provider": sorted(by_provider.values(), key=lambda x: -x["cost"]),
    }
