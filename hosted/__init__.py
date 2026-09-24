"""Hosted waku: one VM, one container per person, the stock dashboard.

This package is a DEPLOYMENT of waku, not a part of it. It never ships in the
wheel or the sdist (pyproject.toml excludes "/hosted"), and it never imports
`waku` — the two meet only over HTTP. Importing `waku.config` would run
find_dotenv(usecwd=True), which walks up from the working directory and loads
the first .env it finds; on the VM that is the platform's own secrets.
evals/deterministic/test_hosted_boundary.py enforces both directions.

See hosted/README.md, and specs/001-hosted-waku/spec.md in the maintainers'
context repo.
"""
