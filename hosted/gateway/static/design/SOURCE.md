# Where these files come from

Copies of `waku/ops/static/design/`, byte for byte. Do not edit them here and
do not edit them there: the master is Waku Memory, and
`waku/ops/static/design/SOURCE.md` says how it is synced.

## Why a copy at all

The services image's build context refuses `waku/`
(`hosted/image/services.Dockerfile.dockerignore`). That refusal is what makes
`import waku` inside `hosted/` a build failure rather than a boundary
violation nobody notices, and it is worth more than the duplication it costs
here. The sign-in page is served by the gateway, so it can only read files
that ride in this tree.

`../fonts/` is the same arrangement: the faces `fonts.css` names, copied from
`waku/ops/static/fonts/` with their SIL Open Font License texts beside them.

`evals/deterministic/hosted/test_login_page.py` compares every one of these
files with its `waku/` original and fails if they differ, so the copy cannot
drift in silence. When you sync the design system, run that eval.
