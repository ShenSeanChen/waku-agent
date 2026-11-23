---
name: seed-issues
description: Keep five or more small, clearly written tasks open for outside contributors. Count the unclaimed `good first issue` issues, draft new ones from what the last reviews and releases turned up, and file them on Sean's yes. Use at the start of a maintainer session after the community queue, after a batch of pull requests is merged or closed, or when the user says seed issues, good first issues, tasks for the community or what can contributors pick up.
---

## Procedure

1. **Count what a newcomer can pick up today.** An issue counts when it carries
   `good first issue`, nobody is assigned, and no open pull request closes it.

   ```bash
   gh issue list --state open --label "good first issue" --json number,title,assignees,comments
   gh pr list --state open --json number,title,closingIssuesReferences
   ```

   With five or more, report the count in one line and stop. With fewer, go on.

2. **Collect candidates from work that already happened.** Do not invent work.
   Look in these places, in this order:
   - Review replies that named a lasting fix and did not build it ("a check
     that asks each listed model for an answer").
   - Pull requests that were declined with an invitation ("a version that
     identifies itself as Waku").
   - Rows of the "Known broken" table in `docs/status.md` that have no fix in
     flight.
   - Claims the docs mark as untested, such as anything about Windows.
   - A fix that covered one provider, channel or tool and left its siblings.

3. **Keep only tasks a stranger can finish in one sitting.** A candidate stays
   when all of these are true:
   - It does one job, and a reviewer can read the whole change in ten minutes.
   - It names the file or files to change.
   - It can be tested offline with a deterministic eval, or the issue says
     which API key the contributor needs.
   - It stays inside `waku/`, `skills/`, `evals/` or `docs/`. Nothing under
     `hosted/`, and nothing that needs access only a maintainer has.
   - It sits as low on the footprint ladder as the job allows
     (`docs/context/conventions.md`).

4. **Write each issue so that nobody has to ask a question.** Use these five
   parts, and follow `docs/context/writing-rules.md`:
   - **What is wrong or missing**, in one sentence a new reader understands.
   - **Where**: the file, and the function when there is one.
   - **What done looks like**: the behaviour, and the test that proves it.
   - **What to leave alone**: the neighbouring thing a contributor is likely
     to change by mistake.
   - **How to try it**: the command to run.

5. **Show Sean the drafts and wait.** An issue is a public promise that a
   pull request for it will be reviewed. File only the ones Sean approves,
   each with `good first issue`, and `help wanted` when it needs an API key.

6. **Answer a claim within two days.** When someone writes "I'll take this",
   reply and assign them. A claim holds for seven days. After seven days
   without a pull request, ask once, then release the issue.

7. **Close the loop when the work merges.** Thank the person by name on the
   pull request, say how the change was checked, and name them in the next
   release notes.

## Why a skill, not a reminder

On 2026-11-22 the repo had 46 open outside pull requests. Forty of them had
never received a reply, and the oldest was three months old. In the two days
of answering them, the reviews produced five tasks that nobody had written
down. A contributor who finds no small task open leaves, and a maintainer who
is busy shipping forgets to write one. Step 1 asks the question every session.
