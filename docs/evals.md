# Evals and tracing

The LLM-Ops pillar: two kinds of eval, a third tier that needs Docker, a
release gate that needs the first two, and a trace of every turn.

## Two kinds of eval, never mixed

*"Did it create the right calendar event?"* is a unit test: 0 or 1, and no
model judges it. *"Was the reply helpful?"* is a judged score with a threshold.
Conflating the two is the most common eval mistake, so here they are separate
suites you can diff.

```bash
make eval          # deterministic: "did the right tool fire?" — 0 or 1, no model judges it
make eval-judge    # LLM-as-judge: "was the reply helpful?" — a scored %, needs a key
make gate          # the release gate: deterministic must pass 100%, judge must clear threshold
```

Deterministic tests are plain pytest in
[`evals/deterministic/`](../evals/deterministic); judged ones use DeepEval in
[`evals/judge/`](../evals/judge). CI runs the deterministic tier on every PR.
The judge tier needs an API key, so `make gate` runs it locally.

There is a third directory, [`evals/hosted_docker/`](../evals/hosted_docker):
0/1 and offline like the deterministic tier, but it needs a Docker daemon, so
it runs in its own `hosted-docker` CI job and not in `make gate`. It is only
for `hosted/`, the deployment that runs waku for other people on a server. With
no daemon, it skips the whole directory and says why.

**Where the results show:** the terminal, and the dashboard's **Evals** page,
under Observability in the sidebar: below "Your turns" (the turn checks), how many deterministic tests and judge
suites exist, what the last `make gate` run passed and failed per suite, the
release-gate verdict, and an eval-history table with one row
per `make gate`. On agent.waku.one the page says that evals run in CI and in
`make gate` before an upgrade, because a tenant container ships no `evals/`.

## Turn checks: evals for your own turns

The suites above grade the Waku Agent code. Five turn checks grade the turns
you run, from the trace each turn already wrote (spec 015,
[`waku/ops/turn_evals.py`](../waku/ops/turn_evals.py)). Each one answers pass,
fail or n/a with a one-line note:

| Check | Passes when |
|---|---|
| `spend_claim` | every dollar figure, "free" or "no cost" the reply states for this turn matches the trace's treg dollars, or the named provider's own calls ("LeadsForge's free preview ($0.00)"); "four Aviato calls at $0.01 each" states $0.04 |
| `one_report` | a research turn saved exactly one report, or its reply carries the report when no Waku Memory is connected. The model's own `memory_remember` of a report counts as a save. A question answered from memory alone (no other tool ran, no save asked) is n/a, and so is a turn whose message asks for no report ("Just answer, no report", "don't save"); saving one anyway fails |
| `grounded_numbers` | 90% of the reply's money amounts, percentages, dates and numbers of two or more digits appear in a tool output, a memory or your message ($1.58M and 1575000, 275K and 274,812 match). The turn's own date is not counted. What the trace says a treg call cost (`treg_call` or `treg_catalog_call_*`) counts as a source, and a JSON answer kept as text inside another is read decoded. A turn that ran only Waku Memory is n/a rather than failed: its numbers may come from the chat's earlier turns, which the trace does not keep |
| `errors_handled` | every failed tool call was retried, or the reply names it ("YouTube" for a `tikhub.youtube.*` call) or says it failed or did not run |
| `under_budget` | the turn cost at most `WAKU_TURN_BUDGET_USD` (default $1.00) |

The dashboard runs the checks each time it reads your traces, so an old turn
is graded the first time the page opens and a fixed check re-grades every
turn. Nothing is written for a code check. A turn's row on Observability →
Turns carries one chip, "5 of 5 checks" or the first failure ("spend claim:
fail"), and its waterfall shows every check with its note. Evals → Your
turns counts passed, failed and n/a per check for today, 7 days or all, with
the newest failing turn. In a terminal, `waku evals turns --window 7d` prints
the same table and every failing turn. A check grades a finished turn: it
never stops or rewrites a reply, and a failed check is not shown in the chat.
A check fails only when a reader of the trace would agree; when the trace
cannot settle it, the check says n/a and its note says why.

One more check runs only when you ask. "Judge this turn" in a turn's
waterfall sends that stored turn's message, reply and tool outputs to the
small model of the provider you use now (Haiku on the hosted free tier) and
asks whether the reply answers the question and stays grounded in the tool
outputs. The button shows the estimated cost first ("about $0.002 with
claude-haiku-4-5"). The answer is written once to the trace as a `score`
event (`answer_grounded`, 0 to 1, passing at 0.7) with the model that judged
and what the call cost, and the chip reads "judge · answer grounded 0.3".
On the hosted free tier the call goes through the metering proxy, so it is
charged in Waku Memory credits like any model call; with your own key it is
charged to that key. The route (`POST /api/turn-evals/judge`) reads the turn
from your own traces, makes one call of at most 300 output tokens, and
refuses a second judge of the same turn within 60 seconds. The judge is often
the same model family as the agent, which favours its own answers, so the
note names the model. `evals/judge/test_turn_judge.py` checks the judge
itself in `make gate`.

## Catching bugs

When you catch a bug by using the thing live, you fix it AND add a
deterministic case so it can never come back. A real example from this repo:
the agent didn't know the current *time* and asked for it before scheduling
"in 30 minutes". The fix is in [`session.py`](../waku/runtime/session.py), and
[`test_working_memory.py`](../evals/deterministic/test_working_memory.py) locks
it in. Run `make gate` → green → the eval history records the run.

## Spend is permanent

Every LLM call's tokens are appended to `~/.waku/usage.jsonl`, an append-only
ledger that a demo reset never wipes. The **Spend** tab of the
**Observability** page shows the all-time cost and tokens, broken down per
model and per day. Tokens × list price is the estimate. On agent.waku.one each turn's receipt also records what the platform
charged, and for that turn the charge replaces the estimate. The Spend card and tab lead with one
total, "$X total · $Y charged + $Z estimated", and the model, treg and memory split adds up to it.

## Tracing is always on

Every turn appends readable lines to `~/.waku/traces/<date>.jsonl` with zero
setup. A trace is the record of one turn: its steps in order, each with its
input, output, time and cost. The **Turns** tab of the **Observability** page
lists the turns and opens each one into a waterfall of its steps, and the
**Tools** tab counts every tool call by where it went (treg with each
endpoint and its cost, Waku Memory, local and MCP tools). Each step carries a
span kind: `llm`, `tool`, `retrieval`, `memory_write` or `gate`. The OTel
export below uses the OpenTelemetry GenAI attribute names
(`gen_ai.operation.name`, `gen_ai.usage.input_tokens`, `gen_ai.tool.name`, …),
mapped in one table, `GENAI` in `waku/ops/observability.py`.

For span-waterfall views in Phoenix:

```bash
pip install -e '.[tracing]'
make trace                                            # Phoenix at localhost:6006
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 make run
```

Langfuse cloud speaks the same OTel toggle.
