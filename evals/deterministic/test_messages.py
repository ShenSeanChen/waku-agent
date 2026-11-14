from datetime import UTC, datetime, tzinfo
from pathlib import Path

import pytest

from waku.tools import messages


@pytest.mark.parametrize("recipients", [("Alex", "Alex"), ("Alex@example.com", "Alex-example-com")])
def test_send_message_preserves_drafts_created_in_the_same_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recipients: tuple[str, str]
) -> None:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return cls(2026, 11, 14, 12, 0, 0, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(messages, "datetime", FrozenDatetime)
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    tool = messages.make_tool(tmp_path)

    results = [tool.fn(to=recipient, body=body) for recipient, body in zip(recipients, ("First draft", "Second draft"))]

    drafts = list(outbox.glob("*.txt"))
    assert len(drafts) == 2
    assert {draft.read_text(encoding="utf-8") for draft in drafts} == {
        f"To: {recipients[0]}\n\nFirst draft\n",
        f"To: {recipients[1]}\n\nSecond draft\n",
    }
    for draft in drafts:
        assert any(draft.name in result for result in results)
