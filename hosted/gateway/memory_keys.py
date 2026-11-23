"""Mint a person's Waku Memory API key with their own sign-in token (spec 004).

Waku Memory's `POST /keys` accepts only a person's own Supabase token and
refuses an API key, so a key minted here can never mint another. The gateway
holds exactly that token for the length of one `POST /auth/session`, and its
audience is Waku Memory's own MCP address, so the API base is that audience
with `/mcp` removed: no new setting, and no way for the two to disagree.
"""

from __future__ import annotations

import aiohttp

from hosted.core.tenant import is_memory_key

LABEL = "Waku Agent (hosted)"
TIMEOUT_SECONDS = 10


def api_base(audience: str) -> str:
    """`https://api.waku.one/mcp` -> `https://api.waku.one`."""
    base = audience.rstrip("/")
    return base.removesuffix("/mcp")


class WakuMemoryKeys:
    def __init__(self, session: aiohttp.ClientSession, audience: str) -> None:
        self._session = session
        self._url = api_base(audience) + "/keys"

    async def mint(self, access_token: str) -> tuple[str, str] | None:
        async with self._session.post(
                self._url, json={"label": LABEL},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)) as response:
            if response.status != 201:
                return None
            body = await response.json()
        key, key_id = body.get("plaintext"), body.get("id")
        # Checked here and again at the spawner: a key that is not the shape
        # Waku Memory mints is never stored, so it can never reach a container.
        if not is_memory_key(key) or not isinstance(key_id, str):
            return None
        return key, key_id

    async def is_live(self, access_token: str, key_id: str) -> bool | None:
        try:
            async with self._session.get(
                    self._url, headers={"Authorization": f"Bearer {access_token}"},
                    timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)) as response:
                if response.status != 200:
                    return None
                body = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return None
        keys = body.get("keys") if isinstance(body, dict) else None
        if not isinstance(keys, list):
            return None
        for key in keys:
            if isinstance(key, dict) and key.get("id") == key_id:
                return key.get("revoked_at") is None
        return False
