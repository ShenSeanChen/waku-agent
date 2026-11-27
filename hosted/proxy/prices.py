"""What a model call costs, in dollars. One table, so a price change is one edit.

Claude rates are Anthropic's first-party list prices per million tokens, from
the claude-api skill's model table (cached 2026-09-25). A five-minute cache
write is 1.25x input and a cache read is 0.05x on Sonnet 5.5 ($0.10, spec 039
F14) and 0.1x on Haiku 4.5. The proxy accepts only the five-minute
`cache_control` (admission.py), and every usage field is priced: a field left
out is spend nobody pays for.
"""

from __future__ import annotations

from typing import NamedTuple

PER_MILLION = 1_000_000
# Spec 001, step 7: the counted input plus 10%, because the count is an estimate.
INPUT_MARGIN = 1.10


class Price(NamedTuple):
    input: float          # $ per million input tokens
    output: float         # $ per million output tokens (thinking counts as output)
    cache_write: float    # $ per million cache_creation_input_tokens
    cache_read: float     # $ per million cache_read_input_tokens


PRICES: dict[str, Price] = {
    "claude-sonnet-5-5": Price(2.00, 10.00, 2.50, 0.10),
    "claude-haiku-4-5": Price(1.00, 5.00, 1.25, 0.10),
    # Spec 043: the Bedrock fallback. List price from third-party price pages
    # (spec 043, F6); Bedrock has no prompt cache for it, so both cache rates
    # are the input rate rather than zero.
    "zai.glm-5": Price(1.00, 3.20, 1.00, 1.00),
}


def cost(model: str, usage: dict) -> float:
    """Dollars for one call's usage block. A missing field is zero tokens."""
    price = PRICES[model]

    def tokens(name: str) -> int:
        value = usage.get(name, 0)
        return value if isinstance(value, int) and value > 0 else 0

    return (tokens("input_tokens") * price.input
            + tokens("output_tokens") * price.output
            + tokens("cache_creation_input_tokens") * price.cache_write
            + tokens("cache_read_input_tokens") * price.cache_read) / PER_MILLION


def worst_case(model: str, *, input_tokens: int, max_tokens: int,
               cached_tokens: int = 0, writes_cache: bool = False) -> float:
    """The reservation: counted input plus the margin, and every output token.

    Spec 039 B2. `cached_tokens` are the input tokens a warm prompt cache will
    serve, reserved at the cache read price, so a person near their limit is
    not refused for tokens that cost a twentieth of the input price. When the
    call carries a breakpoint and no warm cache is known (`writes_cache`), the
    rest may be written to the cache at 1.25x input, above the 10% margin, so
    it is reserved at the write price."""
    price = PRICES[model]
    cached = max(0, min(cached_tokens, input_tokens))
    rest_rate = INPUT_MARGIN * price.input
    if writes_cache:
        rest_rate = max(rest_rate, price.cache_write)
    return (cached * price.cache_read + (input_tokens - cached) * rest_rate
            + max_tokens * price.output) / PER_MILLION
