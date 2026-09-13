# Local OpenCode provider

Waku can use an OpenCode HTTP server through `WAKU_PROVIDER=opencode_local`.
OpenCode manages provider credentials and model access; Waku keeps its own
tool execution and conversation loop.

Install OpenCode using its [installation instructions](https://opencode.ai/docs/),
for example:

```sh
npm install -g opencode-ai
```

Configure any required provider sign-in inside OpenCode. Confirm that
`opencode/muse-spark-1.3-contributor-free` appears in OpenCode's `/models` picker
and is accessible to your account. The [OpenCode Zen catalog](https://opencode.ai/docs/zen/)
lists this contributor model as free; access and availability can change.

Start the [headless server](https://opencode.ai/docs/server/) on loopback:

```sh
opencode serve --hostname 127.0.0.1 --port 4096
```

In another terminal, run Waku with these settings, or put them in Waku's `.env`:

```sh
export WAKU_PROVIDER=opencode_local
export OPENCODE_SERVER_URL=http://127.0.0.1:4096
export WAKU_MODEL=muse-spark-1.3-contributor-free
export WAKU_SMALL_MODEL=muse-spark-1.3-contributor-free
waku dashboard
```

The URL and both model ids above are the defaults, so only `WAKU_PROVIDER` is
required when OpenCode is already running there. No provider API key is needed
in Waku. Credentials remain in OpenCode; do not copy them into Waku's `.env`.
This adapter expects an unauthenticated loopback server; it does not configure
OpenCode's optional HTTP Basic authentication.

The Models page lists connected providers from the selected server's
`GET /provider` catalog. Unqualified model ids use OpenCode's `opencode`
provider. An explicit `providerID/modelID` chooses another provider configured
inside that server. Waku preserves that selection and never switches models
automatically. An explicit `WAKU_BASE_URL` (or `Settings.base_url` in Python)
takes precedence over `OPENCODE_SERVER_URL`.

Local responses have a default deadline of 1800 seconds. Set
`WAKU_LLM_TIMEOUT=3600` to allow an hour, or `WAKU_LLM_TIMEOUT=0` to remove the
response deadline. Individual HTTP requests still have a finite timeout.
Owned-session abort/deletion uses separate bounded cleanup time. Other
providers retain their existing 120-second default.

The adapter submits each attempt once and polls for a completed reply; it does
not stream partial text. OpenCode's native tools are disabled for each attempt,
and Waku executes the requested Waku tool through its existing loop. A malformed
tool action can receive up to two protocol repair attempts, within the same
response deadline. The prompt API does not accept Waku's `max_tokens` setting,
so the selected provider's output limit applies; truncated replies are rejected.

Free pricing does not mean unlimited capacity. If OpenCode reports a provider
limit or `free_tier_limit`, the request fails with that reason; wait for access
to recover before retrying. A catalog listing alone does not establish current
quota. The exact Muse contributor id has zero default pricing; other models use
their reported catalog prices or Waku's estimated rates.

A quota denial is remembered for that model by the current client, preventing
memory-gate or graph fallback paths from submitting the same unavailable model
again. A future retry timestamp reported by OpenCode releases this hold when
reached; it does not promise that the next call will succeed. If no usable
future timestamp is reported, restart Waku after verifying access has recovered.
Python callers can explicitly clear the hold with
`client.clear_quota_hold("muse-spark-1.3-contributor-free")`.
