"""DETERMINISTIC EVAL — the delegated-coding workspace (waku.tools.workspace).

Scripts pi writes must land somewhere dated + documented, not a temp dir, and
auto-run. These tests pin: the dated folder layout, entry-script detection, a
REAL auto-run (a tiny script we control), the manifest, and the disable switch."""

from __future__ import annotations

from datetime import datetime

from waku.tools import workspace as ws


def test_run_folder_is_dated_and_named(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_WORKSPACE", str(tmp_path))
    when = datetime(2026, 7, 19, 12, 48, 31)
    folder = ws.new_run_folder("kimi-k3", "build me a snake game", now=when)
    assert folder.parent.name == "2026-07-19"          # dated dir
    assert folder.name == "124831-kimi-k3-build-me-a-snake"  # time-model-slug
    assert folder.is_dir()


def test_autorun_runs_the_entry_and_captures_output(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_WORKSPACE", str(tmp_path))
    folder = ws.new_run_folder("m", "print hello", now=datetime(2026, 7, 19, 1, 2, 3))
    (folder / "main.py").write_text("print('hello from the script')\n")
    entry, code, out, _secs = ws.autorun(folder)
    assert entry == "main.py" and code == 0
    assert "hello from the script" in out
    assert (folder / "run.log").exists()


def test_autorun_picks_main_over_a_helper(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_WORKSPACE", str(tmp_path))
    folder = ws.new_run_folder("m", "t", now=datetime(2026, 7, 19, 1, 2, 4))
    (folder / "helper.py").write_text("X = 1\n")
    (folder / "main.py").write_text("print('main ran')\n")
    entry, _code, out, _ = ws.autorun(folder)
    assert entry == "main.py" and "main ran" in out


def test_autorun_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("WAKU_DELEGATE_AUTORUN", "0")
    folder = ws.new_run_folder("m", "t", now=datetime(2026, 7, 19, 1, 2, 5))
    (folder / "main.py").write_text("print('hi')\n")
    assert ws.autorun(folder) is None


def test_autorun_none_when_nothing_runnable(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_WORKSPACE", str(tmp_path))
    folder = ws.new_run_folder("m", "t", now=datetime(2026, 7, 19, 1, 2, 6))
    (folder / "notes.txt").write_text("no python here\n")
    assert ws.autorun(folder) is None


def test_manifest_documents_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_WORKSPACE", str(tmp_path))
    folder = ws.new_run_folder("kimi-k3", "make a game", now=datetime(2026, 7, 19, 1, 2, 7))
    (folder / "game.py").write_text("print('ok')\n")
    files = ws.created_files(folder)
    run = ws.autorun(folder)
    ws.write_manifest(folder, "kimi", "kimi-k3", "make a game", files, run)
    text = (folder / "MANIFEST.md").read_text()
    assert "kimi:kimi-k3" in text and "make a game" in text
    assert "game.py" in text and "Auto-run" in text
