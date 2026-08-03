---
name: pr-worktree
description: >
  Create and tear down the throwaway git worktrees used to test community PRs
  for waku-agent, without leaking API keys or leaving branches behind. Use when
  setting up to test a PR, and ALWAYS after a PR is merged, closed, or parked.
---

# Throwaway worktrees for PR testing

Testing a contributor's PR must never disturb the live dashboard on 7777, and it
must never leave a second copy of Sean's API keys on disk. Both have happened.

## Setting one up

```bash
REPO=$(git rev-parse --show-toplevel)
N=<pr-number>
git -C $REPO fetch -q origin pull/$N/head:pr-$N
git -C $REPO worktree add -q ~/Developer/waku-prs/pr$N pr-$N
ln -sfn $REPO/.waku ~/Developer/waku-prs/pr$N/.waku   # real memory/traces/calendar
```

Never `gh pr checkout` — it switches the branch of the main working tree, and the
live dashboard runs from there.

### Only link `.env` when the test actually needs keys

Deterministic evals do not. Skip the symlink for a test-only pass, so untrusted
contributor code never runs against real credentials. Link it only after you have
read the diff:

```bash
ln -sfn $REPO/.env ~/Developer/waku-prs/pr$N/.env
```

### The symlink trap (this cost us a full copy of every key)

`python-dotenv`'s `set_key` — which the dashboard's Save & switch calls —
**replaces a symlinked `.env` with a regular file**. The moment anyone saves a
setting from a PR worktree's dashboard, that worktree gains a complete copy of
every key in the real `.env`: Anthropic, OpenAI, Gemini, Moonshot, xAI, Zhipu,
Tavily, Telegram, Discord.

So: after any test that saved settings, assume the `.env` is a real file and
**check before deleting the worktree**, then move anything worth keeping into the
main `.env` rather than losing it.

```bash
[ -L ~/Developer/waku-prs/pr$N/.env ] && echo "symlink (safe)" || echo "REAL FILE — copy of all keys"
```

New env vars a PR introduces belong in the **main** `.env`, not the worktree's.
Copy the values across without printing them, and leave switches that would change
what the live demo shows (e.g. `WAKU_EPISODIC_STORE`) on their old value — Sean
films against the local data.

## Running its dashboard

Always a second port, so 7777 stays untouched:

```bash
cd ~/Developer/waku-prs/pr$N
WAKU_DASHBOARD_PORT=7778 $REPO/.venv/bin/python -m waku.ops.dashboard
```

Optional extras a PR needs (`[notion]`, `[discord]`, …) install with `uv`, since
the venv has no `pip`:

```bash
uv pip install --python $REPO/.venv/bin/python "<package>"
```

## Tearing it down — do this the same turn the PR is decided

A worktree is finished the moment the PR is merged, closed, or parked waiting on
the contributor. Leaving it costs disk, confuses future greps, and may be sitting
on a copy of every key.

```bash
REPO=$(git rev-parse --show-toplevel)
# 1. rescue any new env vars into the MAIN .env first (see above)
# 2. stop anything still running from it
lsof -ti:7778 | xargs kill -9 2>/dev/null
# 3. remove worktrees + their local branches
for d in ~/Developer/waku-prs/*/; do git -C $REPO worktree remove --force "$d"; done
git -C $REPO worktree prune
git -C $REPO branch --list 'pr-*' | tr -d ' ' | xargs -r -n1 git -C $REPO branch -D
# 4. verify nothing is left holding keys
find ~/Developer/waku-prs -name ".env" 2>/dev/null
```

That `find` must print nothing. If it prints a path, the key copy is still there.

## Checklist before you say a PR is done

- [ ] new env vars moved into the main `.env`
- [ ] `find ~/Developer/waku-prs -name ".env"` prints nothing
- [ ] `git worktree list` shows only the main tree
- [ ] no `pr-*` branches left
- [ ] nothing still listening on 7778
- [ ] settings the test changed are back where the live demo expects them

Related: [[review-pr]] for how to review and present the PR itself.
