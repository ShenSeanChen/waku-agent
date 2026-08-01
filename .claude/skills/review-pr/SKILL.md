---
name: review-pr
description: >
  Review an incoming community PR for milli-agent and present it Sean's way —
  bilingual (English + 中文), three fixed sections: what they did & why it
  matters, verdict (merge / change / close), and how to see it visually. Use
  whenever Sean asks to look at, test, triage, or decide on a pull request.
---

# Reviewing a PR, Sean's way

Sean maintains milli-agent solo while a community sends PRs against the
`good first issue` list. He needs to decide fast, without reading diffs, and he
reads/repeats these decisions in both English and Chinese (for the channel).

## The hard rule, before anything else

**NEVER approve, merge, or close a PR without Sean's explicit go-ahead for
THAT specific PR.** Not "I reviewed it and it's good" — an actual yes from him,
per PR, after he has seen the summary.

Approving a *plan* that says "drain the queue" is NOT approval to merge. Neither
is "go", "sure", or "continue" on a broader task. This was violated on
2026-07-26: six PRs were merged off a plan approval, and Sean had tested none of
them himself. His words: *"i have not even tested them myself so i am concerned."*

The correct loop is: review → test → **present a table** (what it does, how it
solves it, exactly how it was tested, what was NOT tested) → wait → he says
merge → merge. Testing together is the point; he is the one who has to live
with the repo.

Post review comments and change requests freely. Merging is his call.

## Non-negotiables

1. **Test before you judge.** Never review from the diff alone. Check the PR out
   in an isolated **git worktree** (never `gh pr checkout` — his dashboard runs
   from the main working tree and a branch switch swaps code under a live demo):
   ```bash
   git fetch -q origin pull/<N>/head:pr-<N>
   git worktree add -q ~/Developer/milli-prs/pr<N> pr-<N>
   ln -sfn <repo>/.milli ~/Developer/milli-prs/pr<N>/.milli   # real runtime data
   ln -sfn <repo>/.env  ~/Developer/milli-prs/pr<N>/.env    # real keys
   ```
   Run its tests AND actually run the feature. **Tear the worktree down the same
   turn the PR is decided** — see the `pr-worktree` skill, which covers the
   `set_key`-replaces-your-symlinked-`.env` trap that once left a full copy of
   every API key in `~/Developer/milli-prs/pr18/`.
5. **Test it, don't just read it.** PR #14 looked correct in review and crashed on
   every message (`asyncio.to_thread` moved `respond()` off the thread that owns
   the SQLite connection). Reviewing from the diff would have merged it.
2. **Run it against real data**, not just the PR's own fixtures — that's how the
   PR #13 bug surfaced (hardcoded `~/.milli/traces`; the real home is `.milli`
   relative to cwd via `load_settings()`).
3. **Check it against the repo's rules** (CLAUDE.md): stdlib + anthropic/openai
   only — new deps must sit behind an optional extra; tests land in
   `evals/deterministic/`; module + test docstrings in the teaching voice; no
   emojis in any UI surface; the arena must never touch real agent state.
4. **Be honest about depth.** If you ran the tests but did not line-by-line audit
   a 600-line adapter, say so.
5. **Never merge or close on Sean's behalf** unless he says so. Recommend.

## Output format — exactly three sections, each bilingual

For every PR, use this shape. Keep English and 中文 tight and equivalent (中文 is
not a literal translation — write it the way Sean would say it on camera).

```
## PR #<N> — <title> (@<author>)

### 1. What they did & why it matters / 他们做了什么，为什么重要
EN: 2–4 sentences. The change in plain language, then the user-visible payoff.
中文：2–4 句。先说改动，再说对用户/对我们有什么好处。

### 2. Verdict / 结论
EN: Merge as-is / Merge with a change / Ask for a rebase / Close (with a reason
    and a way to keep the contributor engaged). Include the evidence you gathered.
中文：合并 / 改完再合 / 让他 rebase / 关掉（说明理由 + 怎么留住这位贡献者）。

### 3. How to see it yourself / 你怎么亲眼看到
EN: Copy-paste commands. CLI PRs run from the worktree; dashboard PRs run on a
    second port so 7777 is untouched:
    `MILLI_DASHBOARD_PORT=7778 .venv/bin/python -m milli.ops.dashboard`
    Say what to look for — the specific visual difference.
中文：可直接复制的命令 + 该看哪里、看什么变化。
```

Finish with a one-line **recommended order** across the open PRs when several are
in flight, and flag anything blocked on Sean (e.g. a first-time contributor's CI
run needs his approval).

## Tone

Direct, evidence-first, no flattery. If two PRs solve the same issue, say which
one wins and *why*, and propose how to keep the losing contributor engaged
(invite the good part of their work as a follow-up PR) — goodwill is the scarcest
resource in a solo-maintained repo.
