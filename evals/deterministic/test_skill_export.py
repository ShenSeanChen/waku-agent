"""DETERMINISTIC EVAL — `waku skill export` copies skills without clobbering.

Claude Code, Codex and Muse Code read the same SKILL.md folders Waku does, so
exporting is a copy. The one way a copy hurts is overwriting a skill someone
edited in the other agent, so a copy that differs is kept unless --force.
"""

from __future__ import annotations

import sys

import pytest

from waku.memory.procedural import exporter

SKILL = "---\nname: {name}\ndescription: {desc}\n---\n\nDo the thing.\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A WAKU_HOME with one user skill; no bundled skills, so counts are exact."""
    monkeypatch.setattr("waku.memory.bundled_skill_dirs", list)
    home = tmp_path / "waku-home"
    folder = home / "skills" / "tidy-inbox"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(SKILL.format(name="tidy-inbox", desc="Tidy the inbox"), encoding="utf-8")
    (folder / "rules.md").write_text("archive newsletters\n", encoding="utf-8")
    return home


def test_copies_the_whole_skill_folder(home, tmp_path):
    lines = exporter.export(["claude"], home, tmp_path / "user")
    dest = tmp_path / "user" / ".claude" / "skills" / "tidy-inbox"
    assert (dest / "SKILL.md").exists() and (dest / "rules.md").exists(), "extra files travel with the skill"
    assert len(lines) == 1 and "copied" in lines[0]


def test_both_targets(home, tmp_path):
    exporter.export(["claude", "codex"], home, tmp_path / "user")
    for agent in (".claude", ".codex"):
        assert (tmp_path / "user" / agent / "skills" / "tidy-inbox" / "SKILL.md").exists()


def test_a_second_run_changes_nothing(home, tmp_path):
    exporter.export(["claude"], home, tmp_path / "user")
    lines = exporter.export(["claude"], home, tmp_path / "user")
    assert "unchanged" in lines[0]


def test_an_edited_copy_is_kept_unless_forced(home, tmp_path):
    exporter.export(["claude"], home, tmp_path / "user")
    edited = tmp_path / "user" / ".claude" / "skills" / "tidy-inbox" / "SKILL.md"
    edited.write_text(SKILL.format(name="tidy-inbox", desc="my own edit"), encoding="utf-8")

    lines = exporter.export(["claude"], home, tmp_path / "user")
    assert "kept yours" in lines[0] and "my own edit" in edited.read_text(encoding="utf-8")

    exporter.export(["claude"], home, tmp_path / "user", force=True)
    assert "Tidy the inbox" in edited.read_text(encoding="utf-8")


def test_a_skill_file_at_a_skills_root_never_copies_the_whole_root(tmp_path, monkeypatch):
    monkeypatch.setattr("waku.memory.bundled_skill_dirs", list)
    home = tmp_path / "waku-home"
    (home / "skills").mkdir(parents=True)
    (home / "skills" / "SKILL.md").write_text(SKILL.format(name="loose", desc="loose"), encoding="utf-8")
    assert exporter.export(["claude"], home, tmp_path / "user") == []


def test_the_bundled_skills_export_by_default(tmp_path):
    """No stubs: whatever ships with Waku exports under its own name."""
    from waku.memory import bundled_skill_dirs
    from waku.memory.procedural.loader import SkillLoader

    shipped = {s.name for s in SkillLoader(bundled_skill_dirs()).skills}
    exporter.export(["claude"], tmp_path / "empty-home", tmp_path / "user")
    copied = {p.name for p in (tmp_path / "user" / ".claude" / "skills").iterdir()}
    assert shipped and shipped <= copied


def test_memory_target_says_it_is_not_available_yet(capsys):
    assert exporter.cli_main(["--to", "memory"]) == 1
    assert "not available yet" in capsys.readouterr().out


def test_unknown_target_is_refused(capsys):
    assert exporter.cli_main(["--to", "cursor"]) == 1
    assert "Choose from: claude, codex" in capsys.readouterr().out


def test_cli_door(home, tmp_path, monkeypatch, capsys):
    from waku.__main__ import main

    monkeypatch.setenv("WAKU_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["waku", "skill", "export", "--project"])
    with pytest.raises(SystemExit) as exit_:
        main()
    assert exit_.value.code == 0
    assert (tmp_path / ".claude" / "skills" / "tidy-inbox" / "SKILL.md").exists()
