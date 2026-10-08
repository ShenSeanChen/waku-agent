---
name: release
description: Cut a waku-agent release when main is ahead of the last tag. Check the gap, bump the version, tag, confirm PyPI, announce. Use when the last release is 14 or more days old, when 10 or more commits sit past the last tag, or when the user says release, cut a release, bump the version or tag.
---

## Procedure

1. **Measure the gap.** This shows the last release and how far `main` has moved.

   ```bash
   git fetch origin --tags -q
   gh release list --limit 1
   git log "$(git describe --tags --abbrev=0 origin/main)"..origin/main --oneline | wc -l
   ```

   Release when the last release is 14 or more days old and a user-visible change
   has landed since, or when 10 or more commits have landed regardless of age.
   Otherwise report the gap in one line and stop.

2. **Propose the version and the notes, then wait.** Propose the next patch
   version (`0.1.N` to `0.1.N+1`) unless Sean names another. List the 3 to 5
   changes a user would notice, in plain words, from `git log <last tag>..origin/main`.
   Sean says go before anything below happens.

3. **Bump the version in one file.** Edit `__version__` in `waku/__init__.py`
   and nowhere else: `evals/deterministic/test_version.py` goes red if a second
   home appears. Ship it as its own PR with the `ship` skill: branch, `make lint`,
   `make gate`, `gh pr create --fill`, `gh pr checks --watch`, squash-merge.

4. **Tag the merged commit.** Pull `main`, then push the tag. The tag is the
   release: `.github/workflows/release.yml` runs the gate, publishes to PyPI over
   Trusted Publishing and creates the GitHub Release. An agent pushes the tag only
   with Sean's yes for that exact version, and never handles a PyPI token.

   ```bash
   git checkout main && git pull -q
   git tag vX.Y.Z && git push origin vX.Y.Z
   ```

5. **Watch the workflow finish.** `gh run list --workflow release --limit 1`
   must show success. A red run means nothing was published, so fix forward and
   re-tag only if PyPI never received the version (PyPI never accepts a version twice).

6. **Confirm by installing, not by reading the index page.** In a fresh venv,
   install the new version from PyPI, check that `waku.__version__` matches, and
   check that a file new in this release is present.

   ```bash
   d=$(mktemp -d) && python -m venv "$d/v"
   "$d/v/bin/pip" install -U waku-agent==X.Y.Z
   "$d/v/bin/python" -c "import waku; print(waku.__version__)"
   ```

7. **Announce it.** Starring the repo does not subscribe anyone to releases, so
   GitHub notifies only people who watch with Releases selected. Draft these for
   Sean and post nothing yourself:
   - an X post in his voice (lowercase, no dashes, no markdown bold) that leads
     with what changed, not the version number
   - a GitHub Discussions post in the Announcements category with the same notes
   - three lines to prepend to the GitHub Release (`gh release edit vX.Y.Z --notes-file ...`),
     because the generated notes list PR titles and not what a user gains

## Why a skill, not a reminder

`v0.1.3` and `v0.1.4` were tagged and never uploaded, and after `v0.1.8` on
2026-09-13 `main` moved 115 commits in 25 days with no release. Both times the
rule existed and nothing asked whether it was due. Step 1 is that question, and
`docs/context/maintainers.md` asks it at the start of every maintainer session.
