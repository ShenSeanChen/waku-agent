---
name: ship
description: Commit and push the current work properly — lint, run the release gate, write a detailed commit message, push to GitHub. Use whenever work reaches a milestone or the user says ship it, commit, or push.
---

## Procedure

1. `make lint` — fix anything it flags before continuing.
2. `make gate` — the release gate. Deterministic evals must pass 100%; judge
   evals must clear threshold when an API key is present. If the gate closes,
   fix the cause (or, if a finding is a genuine behavior change, update the
   eval WITH the user's agreement) — never push a red gate.
3. Review `git status` and `git diff` — confirm nothing unintended is staged
   (especially nothing from `.waku/` or `.env`).
4. Commit with a detailed message:
   - Subject: imperative, specific ("Fix triple-booking from first live test"),
     never generic ("update code", "fixes").
   - Body: WHY the change exists, what evidence motivated it (live bug, trace,
     eval failure), and what verification it survived.
   - End with: `Co-Authored-By: Claude <the current model's attribution line>`
5. **Push via a branch and a PR — `git push origin main` will be REJECTED.**
   Since 2026-07-26 `main` requires the `skills-and-evals` check to be green
   BEFORE a commit can land, and `enforce_admins` is on, so this applies to Sean
   and to Claude equally. There is no bypass and you should not look for one.

   ```bash
   git checkout -b <short-topic-branch>
   git push -u origin HEAD
   gh pr create --fill                    # body already written in step 4
   gh pr checks --watch                   # ~30s
   gh pr merge --squash --delete-branch   # ONLY Sean's own work, see below
   git checkout main && git pull -q
   ```

   The rejection looks like `GH006: Protected branch update failed` — that is
   the guard working, not a broken setup. Don't retry with `--force`; force
   pushes to main are blocked too.

6. **A community PR is never merged here.** This procedure covers Sean's own
   work. Merging someone else's PR requires his explicit per-PR go-ahead — see
   the `review-pr` skill's hard rule.

7. If CI goes red after a merge, fix forward immediately — don't leave main red.

## Why the extra steps

Eight commits landed on main in one session on 2026-07-26 while
`enforce_admins` was off. Every push printed `Bypassed rule violations` and
went through anyway; CI ran AFTER each one rather than gating it. All eight
happened to be green, so nothing broke — but a red commit would have landed
exactly the same way. CI takes ~30 seconds. The branch-and-PR round trip costs
about a minute and makes a broken main impossible rather than unlikely.
