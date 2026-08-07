"""Regenerate only the marked Connections block in .env.example."""

from __future__ import annotations

import argparse
import difflib
from pathlib import Path

from waku.integrations import render_env_example_block

BEGIN = "# BEGIN GENERATED CONNECTIONS"
END = "# END GENERATED CONNECTIONS"


def replace_block(contents: str, block: str) -> str:
    start = contents.find(BEGIN)
    end = contents.find(END)
    if start < 0 or end < 0 or end < start:
        raise ValueError(".env.example is missing generated Connections markers")
    end += len(END)
    return contents[:start] + block.rstrip("\n") + contents[end:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("--path", type=Path, default=Path(".env.example"))
    args = parser.parse_args(argv)
    path = args.path
    current = path.read_text(encoding="utf-8")
    expected = replace_block(current, render_env_example_block())
    if args.check:
        if current == expected:
            return 0
        print("Generated Connections block is out of date:")
        print("".join(difflib.unified_diff(current.splitlines(True), expected.splitlines(True),
                                            fromfile=str(path), tofile="generated")))
        return 1
    path.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
