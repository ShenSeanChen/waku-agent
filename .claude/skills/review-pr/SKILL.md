---
name: review-pr
description: >
  Walk Sean through an incoming waku-agent PR or issue and present it his way —
  four fixed sections: what this is, why it matters, how HE can test it with you
  as copilot, and are we ready to merge / reply / close and why. Use whenever
  Sean asks to look at, test, triage, or decide on a pull request or an issue,
  and whenever a queue of them is being drained one at a time.
---

# Deciding on a PR or an issue, Sean's way

Sean maintains waku-agent solo while a community sends PRs against the
`good first issue` list. He decides fast, without reading diffs, and he is a
product manager with a technical background — he wants the decision framed for
him AND he wants to be able to see it with his own eyes.

## The hard rule, before anything else

**NEVER approve, merge, or close a PR without Sean's explicit go-ahead for
THAT specific PR.** Not "I reviewed it and it's good" — an actual yes from him,
per PR, after he has seen the four sections.

Approving a *plan* that says "drain the queue" is NOT approval to merge. Neither
is "go", "sure", or "continue" on a broader task. This was violated on
2026-07-26: six PRs were merged off a plan approval, and Sean had tested none of
them himself. His words: *"i have not even tested them myself so i am concerned."*

Post review comments and change requests freely. Merging is his call.

## Frontend and TUI changes are HIS to test

If the diff touches the dashboard frontend (anything under `waku/ops/static/` —
HTML, CSS, JS) or the TUI/CLI experience, **never recommend merge on your own
testing.** Your screenshots, a clean console and a green suite are not enough
evidence for these. Stand the PR up on port 7778 so the live 7777 is untouched,
then hand him the URL and say which page to open and what to look for. The
fourth section becomes "click this, then tell me to merge."

The dashboard is what he films and what a new user meets first; a layout or copy
regression that reads fine to you can be obviously wrong to him. Backend-only
changes he is happy to take on your testing.

## One item at a time

Never batch. One PR or issue, four sections, then **stop and wait** for his
call. Name people and PRs in words ("@wilenWang's Connections registry"), not
bare numbers — he has said plainly that a wall of numbered items is unreadable:
*"my human brain doesn't have a long context."*

## Output format — exactly these four sections, in this order

```
# PR #<N> — @<author>            (or:  # Issue #<N> — @<author>)

### What this is
Plain language. What the change or report actually is, in a few lines. No diff,
no jargon dump. If it touches a mechanism, say what that mechanism does first.

### Why this is important
What breaks, who is affected, what it unblocks. Prefer the concrete failure over
the abstract benefit: "memory silently stopped growing, every turn, for every
Kimi user" beats "improves robustness". If it matters little, say that too.

### How do I test this
Copy-paste commands he can run himself, in fenced bash blocks, one command per
block. Say what you already ran and what you got, then give him the same command
so he can confirm it. Include the ONE test that would actually catch a
regression, not just the green suite. Dashboard PRs run on a second port so 7777
is untouched:
    WAKU_DASHBOARD_PORT=7778 .venv/bin/python -m waku.ops.dashboard
Say what to look for — the specific visual difference. Offer to drive it with
him rather than handing him a wall of steps.

### Are we ready to merge / reply / close — and why
One recommendation with its reason. Not a menu of options. Merge as-is / merge
with a follow-up we push ourselves / ask for a change / close warmly. Name what
you did NOT verify. End by asking for his call on that one item.
```

English by default. Write the 中文 version only when he asks for it (he repeats
some decisions on camera) — and when he does, write it the way he would say it,
not as a literal translation.

## Non-negotiables

1. **Test before you judge.** Never review from the diff alone. Check the PR out
   in an isolated **git worktree** (never `gh pr checkout` — his dashboard runs
   from the main working tree and a branch switch swaps code under a live demo).
   See the `pr-worktree` skill for setup and teardown, including the
   `set_key`-replaces-your-symlinked-`.env` trap that once left a full copy of
   every API key in `~/Developer/waku-prs/pr18/`.

   Note: linking `.waku` into the worktree makes
   `test_runtime_data_is_ignored.py` fail six ways — the gitignore pattern is
   directory-only and does not match a symlink. That failure is your setup, not
   the PR. Skip the link for a test-only pass.

2. **Prove the regression test bites.** If a PR claims a regression test, revert
   the source change inside the worktree and re-run it. A test that passes on
   the unfixed code is worth nothing, and this is not hypothetical: PR #74's
   test passed on broken code, because both paths returned 0 and the broad
   `except` made them indistinguishable from outside.
   ```bash
   git show main:<the/changed/file.py> > <the/changed/file.py>
   .venv/bin/python -m pytest <its/test/file.py> -q
   git checkout <the/changed/file.py>
   ```
   When it doesn't bite, say so plainly, and prefer pushing the real assertion
   ourselves as a follow-up over making a good contributor round-trip.

3. **Test it, don't just read it.** PR #14 looked correct in review and crashed
   on every message (`asyncio.to_thread` moved `respond()` off the thread that
   owns the SQLite connection). Reviewing from the diff would have merged it.

4. **Run it against real data**, not just the PR's own fixtures — that's how the
   PR #13 bug surfaced (hardcoded `~/.waku/traces`; the real home is `.waku`
   relative to cwd via `load_settings()`).

5. **Check it against the repo's rules** (CLAUDE.md): stdlib + anthropic/openai
   only — new deps must sit behind an optional extra; tests land in
   `evals/deterministic/`; module + test docstrings in the teaching voice; no
   emojis in any UI surface; the arena must never touch real agent state; and
   the footprint ladder — every registered tool ships in every prompt.

6. **Be honest about depth.** If you ran the tests but did not line-by-line
   audit a 600-line adapter, say so in the fourth section.

## For issues, not PRs

Same four sections; the fourth becomes reply / file a follow-up / close / leave
open, with the reason. Two extra habits:

- **Check who else claimed it** before acting. Issue #67 had three people say
  "I'll take this" and one of them quietly shipped a PR — the other two needed a
  comment so they stopped working.
- **When an outside analysis criticises waku, verify the criticism against the
  code before agreeing or defending.** Simon Strandgaard's atlas said the
  retrieval gate's accuracy was unmeasured (true — every test is plumbing) and
  that waku has no correction mechanism (false — `manage_memory` edits and
  deletes facts). Conceding the first and correcting the second is what made
  that reply worth reading.

## Tone

Direct, evidence-first, no flattery. If two PRs solve the same issue, say which
one wins and *why*, and propose how to keep the losing contributor engaged
(invite the good part of their work as a follow-up PR) — goodwill is the
scarcest resource in a solo-maintained repo.
