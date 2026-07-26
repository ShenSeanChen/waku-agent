# Filming prompts — copy-paste list

The exact prompts to run in the Arena on camera. **Paste each verbatim**
— the arena matches by exact text to score Completion (a typo → it races but
shows "—" for solved). Turn on **"grade with K3"** to also get the Quality score.

Full battery + expected outcomes: [benchmarks.md §3](benchmarks.md). Metric
meanings + the math: [benchmarks.md §10](benchmarks.md). This file is just the
prompts, in shooting order.

## Act 1 — easy (everyone should pass)

```
Schedule a coffee with Alex next Tuesday at 9am
```
```
Remember that Alex prefers morning meetings
```
```
Send Alex a message that the demo moved to Friday
```
```
What is the capital of France?
```
> The last one is the honest **no-tool** case: a good model answers WITHOUT
> calling a tool (green "solved" = it correctly stayed hands-off).

## Act 2 — hard (where cheap models break — the money segment)

```
I might grab coffee with Alex sometime, we'll see.
```
> Must NOT schedule anything (over-eager trap).

```
Block three 25-minute focus sessions tomorrow morning
```
> Must create **three** events, not one (count precision).

```
Remember that I'm vegetarian, then book dinner with Sam this Thursday at 7pm
```
> Must do **both** — save the note AND book (completeness).

```
Check my calendar for a free 30 minutes this afternoon and schedule a short walk
```
> Must **read** the calendar before scheduling (state-awareness).

```
Book a catch-up with Alex on Friday
```
> The arena auto-seeds the fact "Alex prefers morning meetings" — a good model
> applies it (books a morning slot) instead of ignoring it.

## Act 3 — multi-tool showcase

```
Build me a Kanto starter team around Pikachu: search current competitive picks for a balanced six, remember that Pikachu is my starter, and schedule two team-training sessions this week
```
```
Search for the result of the Spain vs Argentina World Cup final, remember who won, and draft a message to Raj about watching the highlights together
```
> Each needs 3+ tool calls: search + remember + schedule/message.

## Act 4 — the reveal

Scroll to the **Scoreboard**: the cost-vs-quality **scatter** (cheap & good =
top-left), then sort the table by **solved**, **K3 grade**, or **total cost**.

## Act 5 — the coding round (terminal, not the arena yet)

```bash
make shootout-coding RUNS="kimi:kimi-k3 anthropic:claude-opus-4-8 gemini:gemini-3.5-flash"
```
Each model's pi writes real code, scored by tests passing. See the "coding in the
arena" note in [benchmarks.md §3.B](benchmarks.md) for what's built vs. not.

---

# The pi × waku video — shooting rundown

Segment order per Sean's retention call: **philosophy first, machine second,
stakes last** — primitives are sprinkled as 15-second footnotes, never a cold
open. Script backbone: [pi-agent.md](pi-agent.md) — the single walkthrough. Boards: `docs/whiteboards/pi-architecture.excalidraw` +
`pi-vs-claude-code.excalidraw` (open on excalidraw.com to film).

## Segment A — the philosophy (Level 4, cold open)

Board phrases, hand-drawn: "The context window IS the system" · "Nothing
enters that you didn't put there" · "4 tools. 792 lines. 37 providers." ·
"Every refusal became someone else's package."

## Segment B — the machine, live in the terminal (Level 3)

Repo is cloned at `~/Developer/pi` (pinned @ 24e5cc0). Beats, in order:

```
wc -l ~/Developer/pi/packages/agent/src/agent-loop.ts        # → exactly 792
```

```
pi --mode json -p --no-session -nt "Reply with exactly: waku waku"
```
> the naked event stream — point at usage+cost riding on every line.

The 10-line permission system (`guard.ts` in scratch, load with `-e`):
ask pi to `rm -rf ./doomed_dir`, watch the block, `ls doomed_dir` survives.
**Give the block a reason the model can act on** or it retries until timeout —
say that on camera, it's a real lesson.

Session tree: open any `~/.pi/agent/sessions/**/*.jsonl` and show
`{id, parentId}` lines; note even model changes are tree nodes.

## Segment C — two bets on the harness (Level 5, the stakes)

Board 2. The two-command demo, live:

```
claude -p "Reply with exactly: waku waku"     # CLI: answers and dies
claude "Reply with exactly: waku waku"        # TUI: banner, state, waits
```

Then the naked-model story: the real Gemini session that read gpt-5.6 in pi's
own docs and decided it was "a simulated timeline." Punchline: *"Claude Code
carries the model; pi shows you the model. That's why waku's arena delegates
coding to pi."*

## Segment D — waku hires pi (the demo)

```
WAKU_EXPERIMENTAL=1 make run
```
```
help me build a 贪吃蛇 game and run it when you finish
```
> gate → delegate_task → pi on the loop's own model → dated workspace →
> autorun. Then the Arena tab: coding toggle on, same task, every card spawns
> pi on its own brain; sub-agent events stream into the card and pi's tokens
> hit the card's cost (the "no more free coding runs" line).

Receipts: `python -m waku.ops.show_trace` for the delegation in the trace.

## Dry-run checklist (run all of it the day before)

- [ ] `wc -l` shows 792 (repo not accidentally updated)
- [ ] guard.ts demo blocks and survives; block reason is actionable
- [ ] `--mode json` one-liner works on the chosen cheap model
- [ ] snake-game delegation completes and autoruns (pygame installed!)
- [ ] Arena coding race streams sub-agent events into cards
- [ ] every model card shows its knowledge-cutoff label
- [ ] dashboard restarted after the last backend pull
