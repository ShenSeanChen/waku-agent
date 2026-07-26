"""DETERMINISTIC EVAL — procedural skills use a portable text encoding."""

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from waku.memory.procedural import installer
from waku.memory.procedural.loader import SkillLoader
from waku.ops.dashboard import memory_action
from waku.tools.memory_admin import make_create_skill_tool

DESCRIPTION = "\u5904\u7406\u4e2d\u6587\u5468\u62a5"
BODY = "\u7b2c\u4e00\u6b65\uff1a\u603b\u7ed3\u672c\u5468\u3002 \U0001f680"


@pytest.fixture
def require_explicit_text_encoding(monkeypatch):
    """Make implicit writes fail on every OS, as they can on Windows."""
    original_write_text = Path.write_text

    def reject_implicit_encoding(path, data, encoding=None, *args, **kwargs):
        if encoding is None:
            raise UnicodeEncodeError("gbk", "\U0001f680", 0, 1, "illegal multibyte sequence")
        return original_write_text(path, data, *args, encoding=encoding, **kwargs)

    monkeypatch.setattr(Path, "write_text", reject_implicit_encoding)


def assert_utf8_skill(path: Path, name: str) -> None:
    text = path.read_bytes().decode("utf-8")
    assert DESCRIPTION in text and BODY in text
    assert [skill.name for skill in SkillLoader([path.parents[1]]).skills] == [name]


def test_skill_loader_reads_utf8_when_platform_default_cannot(tmp_path, monkeypatch):
    skill_path = tmp_path / "demo" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_path.write_text(
        "---\nname: demo\ndescription: portable skill\n---\nUse an em dash \u2014 safely.\n",
        encoding="utf-8",
    )

    original_read_text = Path.read_text

    def reject_implicit_encoding(path, encoding=None, *args, **kwargs):
        if encoding is None:
            raise UnicodeDecodeError("gbk", b"\x94", 0, 1, "illegal multibyte sequence")
        return original_read_text(path, *args, encoding=encoding, **kwargs)

    monkeypatch.setattr(Path, "read_text", reject_implicit_encoding)

    loader = SkillLoader([tmp_path])

    assert [skill.name for skill in loader.skills] == ["demo"]
    assert "em dash \u2014 safely" in loader.skills[0].body


def test_agent_created_skill_is_written_as_utf8(tmp_path, require_explicit_text_encoding):
    home = tmp_path / "home"
    memory = SimpleNamespace(skills=SkillLoader([home / "skills"]))
    settings = SimpleNamespace(home=home)
    tool = make_create_skill_tool(settings, memory)

    result = tool.fn(name="chinese-report", description=DESCRIPTION, body=BODY)

    assert result.startswith("Created skill 'chinese-report'")
    assert_utf8_skill(home / "skills" / "chinese-report" / "SKILL.md", "chinese-report")


def test_installed_skill_is_written_as_utf8(
    tmp_path, monkeypatch, require_explicit_text_encoding
):
    home = tmp_path / "home"
    source = (
        f"---\nname: installed-report\ndescription: {DESCRIPTION}\n---\n\n{BODY}\n"
    ).encode()
    monkeypatch.setenv("WAKU_HOME", str(home))
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *args, **kwargs: BytesIO(source))

    installer.install("https://example.com/SKILL.md")

    assert_utf8_skill(home / "skills" / "installed-report" / "SKILL.md", "installed-report")


def test_dashboard_edited_skill_is_written_as_utf8(
    tmp_path, monkeypatch, require_explicit_text_encoding
):
    home = tmp_path / "home"
    path = home / "skills" / "dashboard-report" / "SKILL.md"
    path.parent.mkdir(parents=True)
    content = f"---\nname: dashboard-report\ndescription: {DESCRIPTION}\n---\n\n{BODY}\n"
    monkeypatch.setenv("WAKU_HOME", str(home))

    result = memory_action({"action": "save_skill", "path": str(path), "content": content})

    assert result == {"ok": True}
    assert_utf8_skill(path, "dashboard-report")
