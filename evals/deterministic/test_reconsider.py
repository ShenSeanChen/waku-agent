"""DETERMINISTIC EVAL — reconsider reasons over falsified assumptions.

Hermetic, no model, no network. What's pinned: a watch fires only when the
observation contradicts its bound (insufficient evidence is not a
falsification), and the briefing names the claim, the observation vs threshold,
the downstream items to mark stale, and the temporary-vs-structural experiment.
"""

from __future__ import annotations

from waku.ops.reconsider import evaluate_watch, reconsider

TOML = '''\
name = "qamata"
title = "Qamata"

[[watches]]
id = "qamata.assumption.placement-startup"
claim = "Provider A starts a workload in ~18s"
metric = "placement.startup_median"
op = "lt"
threshold = 25
depended_on_by = ["qamata.arch.placement", "qamata.arch.tariff"]
'''


def _project(tmp_path):
    p = tmp_path / "qamata"
    p.mkdir()
    (p / "project.toml").write_text(TOML, encoding="utf-8")
    return p


def test_watch_fires_only_when_the_claim_is_falsified():
    w = {"metric": "m", "op": "lt", "threshold": 25}
    assert evaluate_watch(w, {"m": 18}) == (False, 18.0)   # holds
    assert evaluate_watch(w, {"m": 41}) == (True, 41.0)    # falsified


def test_no_observation_does_not_fire():
    assert evaluate_watch({"metric": "m", "op": "lt", "threshold": 25}, {}) == (False, None)


def test_reconsider_reports_the_fired_watch_and_its_downstream(tmp_path):
    out = reconsider(_project(tmp_path), {"placement.startup_median": 41})
    assert "qamata.assumption.placement-startup" in out
    assert "41.0" in out and "expected < 25" in out
    assert "qamata.arch.placement" in out and "qamata.arch.tariff" in out
    assert "temporary or structural" in out
    assert "1 of 1 watches fired" in out


def test_reconsider_is_quiet_when_the_claim_holds(tmp_path):
    out = reconsider(_project(tmp_path), {"placement.startup_median": 18})
    assert "0 of 1 watches fired" in out


def test_reconsider_with_no_watches_is_honest(tmp_path):
    (tmp_path / "project.toml").write_text('name = "qamata"\n', encoding="utf-8")
    out = reconsider(tmp_path, {})
    assert "No watches recorded" in out


def test_reconsider_walks_the_maps_edges_beyond_the_direct_dependents(tmp_path):
    """The watch names two direct dependents; the map says the tariff feeds the
    runbook. The briefing must name the runbook too — the graph is queried."""
    p = tmp_path / "q"
    p.mkdir()
    (p / "project.toml").write_text(TOML + '''
[[map]]
id = "qamata.arch.tariff"
path = "none"
authority = "decided"
status = "established"
depended_on_by = ["qamata.ops.runbook"]

[[map]]
id = "qamata.ops.runbook"
path = "none"
authority = "working-model"
status = "established"
depends_on = ["qamata.arch.tariff"]
''', encoding="utf-8")
    out = reconsider(p, {"placement.startup_median": 41})
    assert "Downstream (mark stale): qamata.arch.placement, qamata.arch.tariff" in out
    assert "Further downstream, via the map's edges: qamata.ops.runbook" in out
