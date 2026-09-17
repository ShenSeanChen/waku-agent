"""DETERMINISTIC EVAL — the knowledge map, read into prompts and written back.

Hermetic, no model, no network. What's pinned:

  * knowledge_context renders EVERY entry with its authority label, inlines only
    source-of-truth / decided bodies, honours the character budget, and says
    where the rest lives — a proposal never reads as doctrine.
  * record_decision writes the decision file, appends a [[map]] entry that is a
    PROPOSAL (never decided), keeps project.toml parseable, and never collides.
  * downstream walks both edge spellings transitively.
"""

from __future__ import annotations

import tomllib

import pytest

from waku.ops.knowledge import downstream, knowledge_context, record_decision

TOML = '''\
name = "qamata"
title = "Qamata"
thesis = "Compute for agents."

[[map]]
id = "qamata.doctrine"
kind = "doctrine"
path = "PLAN.md"
authority = "source-of-truth"
status = "established"
depended_on_by = ["qamata.sc01"]

[[map]]
id = "qamata.sc01"
kind = "decision"
path = "decisions/SC-01.md"
authority = "decided"
status = "established"
depends_on = ["qamata.doctrine"]

[[map]]
id = "qamata.arch.v1"
kind = "architecture"
path = "architecture/v1.md"
authority = "proposal"
status = "deferred"
depends_on = ["qamata.sc01"]
note = "not authorized"

[[map]]
id = "qamata.old"
kind = "doctrine"
path = "deprecated/"
authority = "history"
status = "established"
'''


def _harness(tmp_path):
    h = tmp_path / "qamata"
    (h / "decisions").mkdir(parents=True)
    (h / "architecture").mkdir()
    (h / "deprecated").mkdir()
    (h / "project.toml").write_text(TOML, encoding="utf-8")
    (h / "PLAN.md").write_text("# Plan\nThe brokerage test binds.", encoding="utf-8")
    (h / "decisions" / "SC-01.md").write_text("# SC-01\nCoding-agent sandbox.", encoding="utf-8")
    (h / "architecture" / "v1.md").write_text("# v1 scope\nNOT AUTHORIZED TEXT", encoding="utf-8")
    return h


def test_context_indexes_every_entry_with_its_authority(tmp_path):
    ctx = knowledge_context(_harness(tmp_path))
    assert "Thesis: Compute for agents." in ctx
    assert "qamata.doctrine [source-of-truth / established] PLAN.md" in ctx
    assert "qamata.arch.v1 [proposal / deferred] architecture/v1.md — not authorized" in ctx
    assert "qamata.old [history / established] deprecated/" in ctx
    assert "A proposal never overrides a source-of-truth" in ctx


def test_context_inlines_only_authoritative_bodies(tmp_path):
    ctx = knowledge_context(_harness(tmp_path))
    assert "The brokerage test binds." in ctx          # source-of-truth: inlined
    assert "Coding-agent sandbox." in ctx              # decided: inlined
    assert "NOT AUTHORIZED TEXT" not in ctx            # proposal: index only
    assert ctx.index("=== PLAN.md") < ctx.index("=== decisions/SC-01.md")  # authority order


def test_context_honours_the_budget_and_points_at_the_rest(tmp_path):
    h = _harness(tmp_path)
    ctx = knowledge_context(h, budget_chars=10)
    assert ctx.count("[truncated — the full text is at") == 2   # both files cut
    assert "Coding-agent sandbox." not in ctx
    assert "qamata.sc01 [decided / established]" in ctx        # the index is never cut
    ctx0 = knowledge_context(h, budget_chars=0)                 # index only
    assert ctx0.count("omitted: context budget exhausted") == 2
    assert "===" not in ctx0


def test_a_long_source_of_truth_cannot_crowd_out_a_short_decision(tmp_path):
    """The Qamata shape: an 80KB PLAN.md beside a 4KB decided record. With a
    shared budget the short record is inlined whole and the long one is cut."""
    h = _harness(tmp_path)
    (h / "PLAN.md").write_text("plan " * 20_000, encoding="utf-8")   # 100KB
    ctx = knowledge_context(h, budget_chars=1000)
    assert "Coding-agent sandbox." in ctx                       # decided: whole
    assert "=== PLAN.md (source-of-truth) ===" in ctx            # doctrine: present
    assert ctx.count("[truncated") == 1                          # ...but cut
    assert "omitted" not in ctx


def test_context_is_empty_without_a_harness_or_map(tmp_path):
    assert knowledge_context("") == ""
    assert knowledge_context(tmp_path) == ""
    (tmp_path / "project.toml").write_text('name = "x"\n', encoding="utf-8")
    assert knowledge_context(tmp_path) == ""


def test_record_writes_a_proposal_the_owner_must_promote(tmp_path, monkeypatch):
    h = _harness(tmp_path)
    path = record_decision(h, "Choose the isolation primitive for v1",
                           "VM-per-run. Residual risk: cold start.")
    assert path.parent == h / "decisions"
    text = path.read_text(encoding="utf-8")
    assert "**proposal**" in text and "VM-per-run." in text and "## Decision" in text

    cfg = tomllib.loads((h / "project.toml").read_text(encoding="utf-8"))
    new = [e for e in cfg["map"] if e["path"] == f"decisions/{path.name}"]
    assert len(new) == 1
    e = new[0]
    assert e["authority"] == "proposal" and e["status"] == "open" and e["owner"] == "harness"
    assert e["id"].startswith("qamata.decision.")
    assert e["kind"] == "decision"
    assert len(cfg["map"]) == 5   # the original four survived the append


def test_record_never_overwrites_and_review_mode_says_verdict(tmp_path):
    h = _harness(tmp_path)
    p1 = record_decision(h, "same task", "first")
    p2 = record_decision(h, "same task", "second", mode="review")
    assert p1 != p2 and p1.exists() and p2.exists()
    assert "## Verdict" in p2.read_text(encoding="utf-8")
    cfg = tomllib.loads((h / "project.toml").read_text(encoding="utf-8"))
    ids = [e["id"] for e in cfg["map"]]
    assert len(ids) == len(set(ids))


def test_record_escapes_quotes_so_the_toml_stays_valid(tmp_path):
    h = _harness(tmp_path)
    record_decision(h, 'a task with "quotes" and a \\ slash', "ok")
    cfg = tomllib.loads((h / "project.toml").read_text(encoding="utf-8"))
    assert any('"quotes"' in e.get("note", "") for e in cfg["map"])


def test_record_records_a_degraded_run_on_the_file(tmp_path):
    h = _harness(tmp_path)
    p = record_decision(h, "t", "d", unavailable={"brain B (reviewer)": "unavailable (rate limit)"})
    assert "Degraded run" in p.read_text(encoding="utf-8")


def test_record_needs_a_harness(tmp_path):
    with pytest.raises(FileNotFoundError):
        record_decision(tmp_path, "t", "d")


def test_downstream_walks_both_edge_spellings_transitively(tmp_path):
    cfg = tomllib.loads(TOML)
    # doctrine → sc01 (via depended_on_by) → arch.v1 (via depends_on)
    assert downstream(cfg["map"], ["qamata.doctrine"]) == ["qamata.sc01", "qamata.arch.v1"]
    assert downstream(cfg["map"], ["qamata.sc01"]) == ["qamata.arch.v1"]
    assert downstream(cfg["map"], ["qamata.arch.v1"]) == []
    assert downstream(cfg["map"], ["not.in.map"]) == []
