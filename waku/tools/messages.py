"""send_message: queue a message in the outbox (spec 023, decision 5).

The tool writes one row into the `outbox` table of state.db and returns. It
never calls Telegram, Slack or GitHub: the dispatcher in `waku serve` sends the
row, and retries it when a delivery fails. Without `waku serve` running the row
waits.

Who it reaches today: the owner (WAKU_OWNER), and the conversation the run
came from. A message to anyone else needs the owner's yes. Approvals are spec
023's group C, so until they exist the tool refuses and writes no row.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from waku.tools.registry import Tool

_FORMS = "telegram:<chat id>, slack:<channel id> or github:<owner>/<repo>#<number>"


def make_tool(home: Path) -> Tool:
    def send_message(to: str, body: str) -> str:
        from waku.alwayson import outbox
        from waku.db import connect

        to, body = (to or "").strip(), (body or "").strip()
        if not body:
            return "Not sent: the message is empty."
        owners = outbox.owner_addresses()
        run = outbox.current_run(home)
        if to.lower() in ("owner", "me"):
            if not owners:
                return ("Not sent: WAKU_OWNER is not set, so Waku does not know where the "
                        f"owner is reached. Set it in .env to {_FORMS}.")
            address = owners[0]
        else:
            address = outbox.parse_address(to)
            if address is None:
                return (f"Not sent: Waku has no address for '{to}'. A message can go to "
                        f"'owner', or to {_FORMS}.")
            if address not in owners and (run is None or address != run.address):
                return (f"Not sent: a message to {to} needs the owner's approval, and Waku "
                        "cannot ask for approval yet. It can message the owner and the "
                        "conversation this request came from.")
        channel, target = address
        key = run.next_key() if run else f"chat:{uuid.uuid4().hex}"
        conn = connect(home)
        try:
            written = outbox.insert(conn, run_id=run.run_id if run else "chat", channel=channel,
                                    target=target, body=body, idempotency_key=key)
        finally:
            conn.close()
        where = outbox.format_address(channel, target)
        if written is None:
            return f"The message to {where} was already queued by an earlier attempt of this run."
        return (f"Queued for {where}. `waku serve` sends it; while `waku serve` is not "
                "running the message waits in the outbox.")

    return Tool(
        name="send_message",
        description=(
            "Send a message through the outbox: it is queued, and `waku serve` delivers it. "
            "`to` is 'owner', or an address: " + _FORMS + ". Use when the user asks you to "
            "message, tell, or remind someone."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "'owner', or an address"},
                "body": {"type": "string", "description": "The message text"},
            },
            "required": ["to", "body"],
        },
        fn=send_message,
    )
