"""What a model call costs, in dollars. One table, so a price change is one edit.

Rates are Anthropic's first-party list prices per million tokens, from the
claude-api skill's model table (cached 2026-09-25). Cache writes are 1.25x
input. The proxy refuses `cache_control`, so the two cache fields are normally
zero, but every usage field is priced anyway: a field left out is spend nobody
pays for.
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
    "claude-sonnet-5-5": Price(2.00, 10.00, 2.50, 0.20),
    "claude-haiku-4-5": Price(1.00, 5.00, 1.25, 0.10),
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


def worst_case(model: str, *, input_tokens: int, max_tokens: int) -> float:
    """The reservation: counted input plus the margin, and every output token."""
    price = PRICES[model]
    return (input_tokens * INPUT_MARGIN * price.input + max_tokens * price.output) / PER_MILLION
