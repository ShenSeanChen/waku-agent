"""Skip this directory when hosted/ is not in the checkout.

The sdist ships evals/ and excludes "/hosted" (pyproject.toml), so a test run
from an unpacked sdist finds these files with nothing behind them. That must
skip, not fail: a contributor who runs the suite from a download should see
"skipped", not an import error from a directory they were never given.

Every test B2, B3 and B4 add here inherits this. None of them has to repeat
it, and none of them may import `hosted` at module scope without it.
"""

from __future__ import annotations

import sys
from pathlib import Path

HOSTED = Path(__file__).resolve().parents[3] / "hosted"

if not HOSTED.is_dir():
    collect_ignore_glob = ["*.py"]
else:
    # gatewaylib.py is a sibling module, not a package.
    # evals/hosted_docker/conftest.py does the same for dockerlib.py.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
