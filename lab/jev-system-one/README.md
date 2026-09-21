# Jev, a System One model

TypeSafe's Jev answers typed questions with calibrated probabilities instead of
writing text. This topic puts it in the five places a harness already makes a
decision, and measures what changes.

## The question

When an agent decides something — which model, which memory, which tool, is
this safe, did it work — does a model that only decides beat a model that
writes a sentence about deciding?

## What we connect

Waku's loop and Waku Memory, to `api.typesafe.ai/v1/systemone`. `jev.py` is the
whole client: sixty lines of stdlib, no SDK, no new dependency. Four suites ask
the same client different questions.

Verified against: jev-1.13.0, 2026-09-20

## Run it

```
export TYPESAFE_API_KEY=...        # get one at typesafe.ai
python lab/jev-system-one/run.py        # Jev on all four suites
python lab/jev-system-one/baseline.py   # the same cases, judged by Haiku 4.5
python lab/jev-system-one/tune.py       # best policy for each, from cached answers
```

Raw answers land in `answers.json` and `answers_llm.json`, so re-tuning a
threshold costs nothing. `baseline.py` needs `ANTHROPIC_API_KEY` too.

Two integrations live in `integrations/`: a Claude Code `PreToolUse` hook, and
an MCP server for agents whose loop you do not own. Both call the same
`jev.py`.

## What we found

- One call answers every question in parallel. Four questions, 244–301 ms.
- $0.02 per 1000 decisions, measured over 44 cases. Output tokens are free.
- `P(angry) = 0.61` and `P(not angry) = 0.44`. Nouls are judged independently
  and do not sum to 1. Never derive one from the other.
- Choice `criteria` is a dict keyed by what your code switches on. Score
  `criteria` is a list, and the legend that comes back is 0-indexed.
- On a 50-command safety gate: one broad question scored 47/50 but missed three
  dangerous commands. Splitting it into three careful narrow questions scored
  **44/50** — worse. Asking the broad question *and* the narrow ones, and
  letting code take the highest, reached 47/50 with nothing dangerous missed.
- Most of our early misses were the policy code, not the model. Moving one
  threshold took the memory suite from 6/12 to 12/12 with no new API calls.
- Against Haiku 4.5 on the same 44 cases, same policy code, each at its own
  best threshold: **39/44 against 33/44**, 260 ms against 570-825 ms, and
  $0.019 against $0.38 per 1000 decisions. That is 2.6x faster and 20x cheaper
  -- not the 193x and 444x TypeSafe advertises, because they timed a frontier
  model writing a full chain of thought and we timed the cheap model you would
  really put in a loop.
- Haiku returned valid JSON on all 44. The parsing failure everyone warns about
  did not happen; the real difference was judgment, latency and price.

## Video angle

Hook: a model that cannot write a sentence makes your agent's decisions for a
fiftieth of a cent per thousand.

The surprising finding: careful question engineering made accuracy go *down*,
and the fix was to stop replacing the naive question and start adding to it.

Boards, in filming order: `screenshots/1-two-shapes.png` (what it is),
`2-where-it-hides.png` (five slots in the loop, and who lets you in),
`3-four-tests.png` (the suites and the scores), `4-what-wed-trust.png` (our
numbers against theirs, and the jaggedness).

## Graduation

**Done, as the Judgment Arena** (`waku/ops/judgment_arena.py`,
`judgment_cases.py`, `static/js/judgment.js`): the third race beside the model
and memory arenas. It holds the harness, the questions and the cases constant
and varies only who answers, so a difference on screen is a difference in
judgment rather than in plumbing.

What moved: the client (one HTTP call, stdlib only, no new dependency, bottom
rung of the footprint ladder) and the suites, rewritten so the unit is the
question. Five cases per suite, three questions written for each — one Noul,
one Choice, one Score — fifteen different questions and fifteen labels, none
repeated. Scoring is per question and there is no policy in between.

What stayed here: the scripts above, as the on-its-own-terms baseline. They
still run without the dashboard, and `tune.py` still re-scores cached answers
offline, which the arena deliberately does not do.
