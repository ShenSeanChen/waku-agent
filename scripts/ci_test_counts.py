"""Print one CI job's test counts as a GitHub notice, read from pytest's junit XML.

    python scripts/ci_test_counts.py "Deterministic evals" /tmp/deterministic.xml

prints

    ::notice title=Deterministic evals::3094 passed, 0 failed, 2 skipped

GitHub turns that line into an annotation on the job's check run, and anyone
can read annotations back through GitHub's public API. That is how the hosted
Evals page gets real counts: hosted/deploy/autodeploy.sh reads this notice for
the commit it deploys and writes it into the release record. The message format
is a contract with autodeploy.sh's jq; change one, change both.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def counts(report: Path) -> dict[str, int]:
    """passed, failed and skipped across every <testsuite> in the report.
    pytest's `tests` includes skipped ones; `failed` counts errors too."""
    root = ET.parse(report).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    total = sum(int(s.get("tests", 0)) for s in suites)
    failed = sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites)
    skipped = sum(int(s.get("skipped", 0)) for s in suites)
    return {"passed": total - failed - skipped, "failed": failed, "skipped": skipped}


def notice(label: str, report: Path) -> str:
    c = counts(report)
    return f"::notice title={label}::{c['passed']} passed, {c['failed']} failed, {c['skipped']} skipped"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: ci_test_counts.py LABEL JUNIT_XML", file=sys.stderr)
        return 2
    print(notice(args[0], Path(args[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
