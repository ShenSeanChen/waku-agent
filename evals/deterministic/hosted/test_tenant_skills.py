"""DETERMINISTIC EVAL -- a provisioned tenant has the research-report skill
(spec 007 A1).

Provisioning writes no skill: the tenant image COPYs skills/ beside waku/
(hosted/image/tenant.Dockerfile), and waku finds it there through
bundled_skill_dirs(). So the check is the one a tenant's waku makes on its
first start: build Memory over the home provisioning wrote, and look.
"""

from __future__ import annotations

from pathlib import Path

from evals.helpers import ScriptedClient
from hosted.core import provision, tenant
from waku.config import Settings
from waku.db import connect
from waku.memory import Memory

ROOT = Path(__file__).resolve().parents[3]


def test_a_provisioned_tenant_has_the_research_report_skill(tmp_path):
    dirs = tenant.tenant_dirs(tmp_path, "abcdefghijkl")
    provision.provision(dirs, ROOT / "hosted" / "templates" / "SOUL.md")
    settings = Settings(home=dirs.home)
    memory = Memory(connect(dirs.home), settings, ScriptedClient([]))
    assert "research-report" in {s.name for s in memory.skills.skills}


def test_the_tenant_image_copies_the_skills_folder():
    dockerfile = (ROOT / "hosted" / "image" / "tenant.Dockerfile").read_text(encoding="utf-8")
    assert "COPY skills ./skills" in dockerfile
