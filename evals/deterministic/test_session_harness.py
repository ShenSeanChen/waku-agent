"""DETERMINISTIC EVAL — the loop knows which project it is bound to.

With WAKU_HARNESS set, every turn's system prompt carries the harness block:
the project's identity, the authority rung the tools are gated to, and the
knowledge map's index with authority labels. Without a harness the prompt is
exactly the classic personal-assistant one. Hermetic: no model, no memory.
"""

from __future__ import annotations

from waku.config import Settings
from waku.runtime.session import Session, harness_block

TOML = '''\
name = "qamata"
title = "Qamata"
thesis = "Compute for agents."

[authority]
default = "recommend"

[[map]]
id = "qamata.doctrine"
kind = "doctrine"
path = "PLAN.md"
authority = "source-of-truth"
status = "established"

[[map]]
id = "qamata.arch.isolation"
kind = "architecture"
path = "none"
authority = "proposal"
status = "open"
note = "VM-per-run vs container"
'''


def _home(tmp_path):
    """Settings.home must exist: load_soul writes SOUL.md there on first run."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return home


def _harness(tmp_path):
    h = tmp_path / "qamata"
    h.mkdir()
    (h / "project.toml").write_text(TOML, encoding="utf-8")
    (h / "PLAN.md").write_text("A very long plan body that must NOT be inlined.", encoding="utf-8")
    return h


def test_bound_session_knows_the_project_and_its_map(tmp_path):
    h = _harness(tmp_path)
    settings = Settings(home=_home(tmp_path), harness=str(h))
    system = Session(settings).build_system("what is still open?")
    assert "bound to the project harness for Qamata" in system
    assert "CTO operating system" in system
    assert "gated to the 'recommend' rung" in system
    assert "qamata.doctrine [source-of-truth / established] PLAN.md" in system
    assert "qamata.arch.isolation [proposal / open] none — VM-per-run vs container" in system
    assert "Thesis: Compute for agents." in system


def test_the_index_only_never_the_bodies(tmp_path):
    """A map, not a copy: the loop has no file tool, so bodies stay out."""
    h = _harness(tmp_path)
    system = Session(Settings(home=_home(tmp_path), harness=str(h))).build_system("hi")
    assert "must NOT be inlined" not in system
    assert "=== PLAN.md" not in system


def test_unbound_session_is_the_classic_assistant(tmp_path):
    settings = Settings(home=_home(tmp_path), harness="")
    system = Session(settings).build_system("hi")
    assert "personal assistant" in system
    assert "project harness" not in system
    assert harness_block(settings) == ""


def test_a_harness_path_without_a_project_toml_binds_nothing(tmp_path):
    settings = Settings(home=_home(tmp_path), harness=str(tmp_path / "nowhere"))
    assert harness_block(settings) == ""
    assert "project harness" not in Session(settings).build_system("hi")
