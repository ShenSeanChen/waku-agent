"""Copy Waku Memory's design files into the dashboard, and record where they came from.

Usage: python scripts/sync_design.py <path to waku-memory-frontend>

tokens.css and controls.css are copied unchanged into waku/ops/static/design/.
SOURCE.md is rewritten with the commit that last changed them and each file's
sha256. evals/deterministic/test_design_system.py checks those hashes, so a
hand edit to a copied file fails CI. To change a token, change it in Waku
Memory, commit it there, then run this."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

DEST = Path(__file__).resolve().parents[1] / "waku" / "ops" / "static" / "design"
FILES = ("tokens.css", "controls.css")

SOURCE = """\
# Where the design files come from

The dashboard uses the Waku Memory design system. Do not edit the copied
files here. Change them in Waku Memory, then run
`python scripts/sync_design.py <path to waku-memory-frontend>`.

## Copied unchanged

From `ShenSeanChen/waku-memory-frontend`, `public/design/`, as of commit
`{commit}` (the last commit that changed them).
`evals/deterministic/test_design_system.py` checks these hashes.

```
{hashes}
```

## Copied by hand

`type.css` holds values Waku Memory keeps in `app/globals.css` rather than in
`tokens.css`: the type scale (`--text-*`), the two weights
(`--font-weight-*`), the line heights (`--leading-*`) and the field border
(`--input`). They were copied from `app/globals.css` at `03ab1d7`. Check them
against that file when you sync.

## Fonts

`../fonts/` holds the Latin subsets Waku Memory loads through next/font, from
Google Fonts. Each face is under the SIL Open Font License; its license is
next to it as `OFL-<face>.txt`.

| File | Face |
|---|---|
| `InstrumentSans-var.woff2` | Instrument Sans, variable, 400–700 |
| `JetBrainsMono-var.woff2` | JetBrains Mono, variable, 100–800 |
| `PlayfairDisplaySC-400.woff2` | Playfair Display SC, 400 |
"""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    root = Path(argv[1]).expanduser().resolve()
    if _git(root, "status", "--porcelain", "--", "public/design"):
        print("refusing: public/design has uncommitted changes, so no commit describes them")
        return 1
    commit = _git(root, "log", "-1", "--format=%h", "--", "public/design")
    hashes = []
    for name in FILES:
        shutil.copyfile(root / "public" / "design" / name, DEST / name)
        hashes.append(f"{hashlib.sha256((DEST / name).read_bytes()).hexdigest()}  {name}")
    (DEST / "SOURCE.md").write_text(SOURCE.format(commit=commit, hashes="\n".join(hashes)))
    print(f"copied {', '.join(FILES)} from {commit}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
