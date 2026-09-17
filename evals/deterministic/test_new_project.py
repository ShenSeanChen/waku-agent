"""DETERMINISTIC EVAL — new-project stamps a project harness skeleton, honestly.

Hermetic: writes only into a tmp_path. What's pinned: the skeleton layout, the
seeded project.yaml carrying the schema's identity + authority + empty map, and
the one refusal that matters — it never overwrites an existing harness.
"""

from __future__ import annotations

import pytest

from waku.ops import new_project


def test_stamp_creates_the_skeleton_and_project_yaml(tmp_path):
    root = new_project.stamp("qamata", dest=str(tmp_path / "qamata"))
    assert root == tmp_path / "qamata"
    assert (root / "project.yaml").exists() and (root / "README.md").exists()
    for d in new_project.SKELETON:
        assert (root / d).is_dir(), f"missing skeleton dir {d}"

    yaml = (root / "project.yaml").read_text(encoding="utf-8")
    assert "name: qamata" in yaml
    assert "authority:" in yaml and "default: recommend" in yaml
    assert "map: []" in yaml and "watches: []" in yaml


def test_stamp_refuses_to_overwrite_an_existing_harness(tmp_path):
    new_project.stamp("qamata", dest=str(tmp_path / "qamata"))
    with pytest.raises(SystemExit) as exc:
        new_project.stamp("qamata", dest=str(tmp_path / "qamata"))
    assert "already exists" in str(exc.value)


def test_main_requires_a_name(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        new_project.main([])
    assert exc.value.code == 1
    assert "usage" in capsys.readouterr().out
