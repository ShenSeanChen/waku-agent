"""The gateway's side of run/proxy/proxy.sock."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hosted import jsonsock


@dataclass(frozen=True)
class Spend:
    month: str
    settled: float
    reserved: float
    last_platform_call: float | None


async def read_spend(socket_path: Path, tenant_id: str) -> Spend | None:
    """None when the proxy cannot answer. The caller then applies free's turn
    limit and /account says the spend is unavailable -- it does not guess."""
    try:
        answer = await jsonsock.ask(socket_path, {"op": "spend", "tenant": tenant_id})
    except (jsonsock.Unreachable, ValueError):
        return None
    if "error" in answer:
        return None
    return Spend(month=answer["month"], settled=answer["settled"],
                 reserved=answer["reserved"],
                 last_platform_call=answer.get("last_platform_call"))
