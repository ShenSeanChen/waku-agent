# Adding a provider: the registry

**Status:** shipped 2026-09-22. Supersedes the "one `PROVIDERS` row" advice in
[conventions §3](context/conventions.md#3-where-new-capability-goes-the-footprint-ladder),
which was true of the code and false of the work.

## The problem

The rulebook said a new provider was "usually one `PROVIDERS` row". The two
provider pull requests open when this was written disagreed:

```
waku/loop/models.py              the row, and a KEY_URLS row
waku/ops/pricing.py              a PRICING row
.env.example                     a generated block, edited by hand anyway
waku/ops/static/logos/<name>.svg the logo
README.md                        a mention
evals/deterministic/…            two to four separate test files
```

Seven to nine files across three directories, four of them tests, for what the
documentation called a one-line change. Nine of the eighteen pull requests open
on 2026-09-22 were providers or model plumbing, the oldest 39 days. They were
not hard to review; they were tedious to review, which is worse, because
tedious review is what gets postponed.

## The change

Providers move out of Python and into `waku/providers.toml`. One table per
provider carrying everything: wire format, key variable, default models,
flagship/fast pair, catalog URL, regional endpoints, where a human gets a key,
and rough pricing.

```toml
[anthropic]
kind = "anthropic"
key_env = "ANTHROPIC_API_KEY"
model = "claude-sonnet-5"
small_model = "claude-haiku-4-5-20251001"
catalog_url = "https://api.anthropic.com/v1/models"
flagship = "claude-opus-4-8"
fast = "claude-sonnet-5"
key_url = "https://console.anthropic.com/settings/keys"
price = [3.0, 15.0]
```

`waku/loop/models.py` builds `PROVIDERS` and `KEY_URLS` from it at import.
`waku/ops/pricing.py` builds `PRICING` from the same rows. `waku/integrations.py`
already derived the `.env.example` block from `PROVIDERS`, so that follows for
free — contributors editing that file by hand were doing work the generator
does.

Adding a provider is now:

1. a table in `waku/providers.toml`
2. `waku/ops/static/logos/<name>.svg`
3. `python scripts/generate_env_example.py` — mechanical, and CI checks it

No test needs editing. `test_providers.py` was already parametrised over
`PROVIDERS`, and `test_providers_registry.py` walks the file: every row
complete, a wire format that exists, a reachable key URL, a logo on disk, a
price, no two providers sharing a key variable, and a regional endpoint that
carries its own env var rather than leaking through the global one.

## Why TOML rather than a Python package

A provider is data. `tomllib` is in the standard library, so this adds no
dependency, and a data file cannot execute anything on import — which matters
when the thing you want is for a stranger's pull request to be safe to merge
on a glance. It also keeps the comments: the reasons a model id is pinned
(gpt-5.5 rather than a `-latest` alias, kimi's missing plain `k2.7`) live
beside the row they explain, which is where they were and where they belong.

## What did not change

`Provider` and `ProviderEndpoint` are the same frozen dataclasses, built with
the same fields. The registry produced by the file was diffed field by field
against the hand-written one before the old list was deleted: identical.

A provider that speaks neither wire format still needs code, and still needs a
proposal. This lowers the cost of the common case; it does not remove the
review from the uncommon one.
