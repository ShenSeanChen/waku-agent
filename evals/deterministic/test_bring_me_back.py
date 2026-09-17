"""DETERMINISTIC EVAL — bring-me-back reconstructs project state from the map.

Hermetic: a tmp project.toml + files, no model, no network. What's pinned: the
thesis surfaces, open decisions rank first, and a file that changed since its
`verified` date is flagged as drift.
"""

from __future__ import annotations

import pytest

from waku.ops.bring_me_back import bring_me_back

TOML = '''\
name = "qamata"
title = "Qamata"
owner = "Proofs Africa"
thesis = "Compute for agents and their actions."
created = "2026-09-14"
updated = "2026-09-14"

[authority]
default = "recommend"
irreversible = []

[[map]]
id = "qamata.doctrine"
kind = "doctrine"
path = "PLAN.md"
authority = "source-of-truth"
status = "established"
owner = "product-owner"
verified = "2020-01-01"

[[map]]
id = "qamata.arch.isolation"
kind = "architecture"
path = "none"
authority = "proposal"
status = "open"
owner = "architect"
note = "VM-per-run vs gVisor"
'''


def _project(tmp_path):
    p = tmp_path / "qamata"
    p.mkdir()
    (p / "project.toml").write_text(TOML, encoding="utf-8")
    (p / "PLAN.md").write_text("# plan", encoding="utf-8")
    return p


def test_reconstructs_thesis_and_open_decision(tmp_path):
    out = bring_me_back(_project(tmp_path))
    assert "# Qamata — bring me back" in out
    assert "Compute for agents and their actions." in out
    # the open decision is the #1 most consequential thing, with its action
    assert "qamata.arch.isolation" in out
    assert "VM-per-run vs gVisor" in out
    assert "resolve `qamata.arch.isolation`" in out


def test_flags_files_drifted_since_verified(tmp_path):
    # PLAN.md verified 2020-01-01 but written "now" → drift is flagged
    out = bring_me_back(_project(tmp_path))
    assert "PLAN.md" in out and "changed since verified" in out


def test_missing_project_toml_is_honest(tmp_path):
    with pytest.raises(SystemExit) as exc:
        bring_me_back(tmp_path)
    assert "no project.toml" in str(exc.value)
