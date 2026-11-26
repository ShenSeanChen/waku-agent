# Proposal for opt-in Anthropic prompt caching

Waku users who call Anthropic directly could set `WAKU_PROMPT_CACHE=1` to
reuse the tool schemas and system prompt across eligible requests. A cache
write costs more than ordinary input, so users with short sessions should
be able to keep the current default. This proposal builds on the
[prototype by snteran](https://github.com/snteran/waku-agent/commit/6184c7fa8de705cddf9f40d15b31e6b6851c4e44)
for [issue #176](https://github.com/ShenSeanChen/waku-agent/issues/176).

## Which requests qualify?

Only the `anthropic` provider at Anthropic's own endpoint would add the
five-minute ephemeral cache markers. Waku would check the resolved endpoint
so a custom proxy can keep its existing request format. The hosted free tier
rejects cache markers, and several other providers use the Anthropic wire format.

For an eligible loop call, Waku would mark a copy of the last tool schema,
when present, and the system text block. Both the streaming call and its
non-streaming fallback would use those blocks. The final call at the step
limit would keep its extra instruction and existing tool choice.

Waku builds the system prompt once per turn. The clock, retrieved memories
and matching skills can change on the next turn. Marking the tools separately
lets Anthropic reuse a sufficiently long tool prefix even when the system
text changes. The provider's response tells Waku whether a cache hit occurred.

## How would a user see the savings?

Each loop call would record ordinary input, output, cache creation and cache
read tokens separately in its trace and usage ledger row. The turn receipt,
observability view and ledger summary would share a cost calculation. At an
input price of $3 per million tokens, a five-minute write of 10,000 tokens
costs $0.0375 and a read of those tokens costs $0.003. Each estimate would
charge ordinary input and output separately. Older ledger rows would
contribute zero cached tokens.

## How would we verify it?

Offline evals would capture the outgoing requests for streaming, fallback
and step-limit calls. They would check that opt-out, custom endpoints and
the hosted provider keep their current payloads, that an empty tool list
works, and that a cached request leaves the registry's tool schemas intact.
A fake provider would report one write followed by one read. The eval would
check both ledger rows and independently computed dollar totals for the
receipt and spend views, including a ledger row from before this change.
