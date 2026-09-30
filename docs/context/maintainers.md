# Maintainers

How the maintainers review PRs, merge and release. Contributors can skip this
file.

`AGENTS.md` deliberately does not import it: a contributor's coding agent must
never start working the maintainer's review queue. A maintainer's own agent
sessions load this file through the maintainer's private memory.

## Talking to Sean

**Be concise.** Sean wants short replies: lead with the answer, and cut
preamble and recap. A few lines beat a wall of text. Expand only when he asks
for detail.

## Start every session by draining the community queue

This is a public repo with contributors waiting, and an unanswered PR teaches
someone that doing what we asked gets silence. So on the first substantive
turn of a new session, before anything else, run:

```
gh pr list --state open  ·  gh issue list --state open
```

Report it as a **short table** (number, title, author, size, CI state, age),
plus anything already visibly wrong: an unrelated lockfile, a "Closes #N" that
points at the wrong issue, a `BEHIND` branch. Then propose an order, **smallest
first**, and stop.

Then walk the queue **one item at a time**, using the `review-pr` skill's four
fixed sections, in this order and no other:

1. **What this is**: plain language, no diff dump
2. **Why this is important**: the concrete failure, not the abstract benefit
3. **How do I test this**: copy-paste commands, and say what I already ran
4. **Merge / modify / close, and why**: one recommendation, then stop

**After each item, stop and wait for Sean's call on that one item.** Never
batch, and never carry one yes forward to the next. Approving a plan is not
approval to merge anything. Test in throwaway worktrees with the `pr-worktree`
skill, never `gh pr checkout`. Frontend and TUI diffs are **Sean's to test**:
stand them up on port 7778 so the live 7777 is untouched, hand him the URL,
and never merge on my own screenshots.

## Merging

- **`main` is protected, and `git push origin main` is rejected for everyone.**
  Since 2026-07-26 a commit only lands once `skills-and-evals` is green, and
  `enforce_admins` is on, so the rule binds Sean and Claude identically. Ship
  with `git checkout -b <topic>` → `gh pr create --fill` → `gh pr checks --watch`
  → `gh pr merge --squash --delete-branch`. `GH006: Protected branch update
  failed` is the guard working; never route around it.
- **Merging a community PR needs Sean's explicit yes for that PR** (see
  `.claude/skills/review-pr/SKILL.md`).
- **Commit and ship every milestone in the same turn.** The moment a change
  works (tests pass, or it is verified live), commit it and get it onto GitHub
  before moving on. Never end a turn or a session with working changes left
  uncommitted: uncommitted work has been lost to branch switches before. Use
  the `/ship` skill, and commit each milestone as its own logical commit.

## Releases: PyPI must never lag `main`

`pip install waku-agent` is how most people meet this project, and for four
weeks it handed them Aug 1 code: `0.1.1` was live while `main` had moved 67
commits. `v0.1.3` and `v0.1.4` were tagged and built and **never uploaded**,
because a tag is not a release. So:

- The version has **one home**: `waku/__init__.py`. `pyproject.toml` reads it
  (`dynamic = ["version"]`), and `evals/deterministic/test_version.py` goes red
  if a second one is ever added.
- When a user-visible change lands on `main`, **cut the release in the same
  session**. Don't let releasable work sit behind an unpublished tag.
- **The release step is `git tag vX.Y.Z && git push origin vX.Y.Z`.**
  `.github/workflows/release.yml` then runs the gate, builds, publishes to PyPI
  over Trusted Publishing (no stored token), and creates the GitHub Release for
  the tag with the same files and generated notes, so the repo page's "Latest"
  always matches PyPI. It refuses if the tag and `__version__` disagree.
- **Sean's PyPI token is never handled by an agent.**
- **Confirm by installing, not by reading the index page**: in a fresh venv,
  `pip install waku-agent` from PyPI, and check that `waku.__version__` matches
  and that a file new in this release is present.

## Syncing the design system

To change a token, change it in Waku Memory's frontend, commit it there, then
run `python scripts/sync_design.py <path to waku-memory-frontend>`. The script
copies `tokens.css` and `controls.css` and records their hashes in
`waku/ops/static/design/SOURCE.md`.
