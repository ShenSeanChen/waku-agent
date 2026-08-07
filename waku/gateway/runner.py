"""One thread-owned Waku per asynchronous message gateway.

Telegram and Discord are async clients, while Waku is deliberately synchronous
and owns a SQLite connection.  A single-worker executor gives each gateway one
stable owner thread: the event loop stays responsive, turns remain serialized,
and SQLite is created, used, and closed on the same thread.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

GATEWAY_ERROR_REPLY = "Waku couldn't process that message. Check the dashboard terminal."


def safe_exception(exc: Exception) -> str:
    """Concise local diagnostic with configured credentials redacted."""
    message = str(exc) or "no details"
    secret_names = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")
    secrets = {
        value for name, value in os.environ.items()
        if value and len(value) >= 4 and name.upper().endswith(secret_names)
    }
    for secret in sorted(secrets, key=len, reverse=True):
        message = message.replace(secret, "***")
    return f"{type(exc).__name__}: {message[:500]}"


class GatewayAgentRunner:
    """Run every turn for one gateway on one dedicated worker thread."""

    def __init__(self, agent_factory: Callable[[], Any], *, session_id: str,
                 source: str, observer: Callable[[str, dict], None] | None = None) -> None:
        self.source = source
        self._session_id = session_id
        self._observer = observer
        self._agent_factory = agent_factory
        self._agent: Any | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"{source}-agent")
        self._state_lock = threading.Lock()
        self._last_error: str | None = None
        self._closed = False

    @property
    def error_status(self) -> str | None:
        with self._state_lock:
            return self._last_error

    def record_error(self, exc: Exception, *, action: str) -> str:
        detail = safe_exception(exc)
        with self._state_lock:
            self._last_error = (
                f"{action} failed ({type(exc).__name__}) — see dashboard terminal"
            )
        return detail

    def clear_error(self) -> None:
        with self._state_lock:
            self._last_error = None

    def _respond(self, text: str):
        try:
            if self._agent is None:
                self._agent = self._agent_factory()
                self._agent.session.session_id = self._session_id
            result = self._agent.respond(
                text, observer=self._observer, source=self.source
            )
        except Exception as exc:
            self.record_error(exc, action="message handling")
            raise
        self.clear_error()
        return result

    async def respond(self, text: str):
        with self._state_lock:
            if self._closed:
                raise RuntimeError(f"{self.source} gateway runner is closed")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._respond, text)

    def _close_agent(self) -> None:
        if self._agent is None:
            return
        agent, self._agent = self._agent, None
        try:
            agent.close()
        finally:
            connection = getattr(agent, "conn", None)
            if connection is not None:
                connection.close()

    def close(self) -> None:
        """Close owned resources on their worker, then retire that worker."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._executor.submit(self._close_agent).result(timeout=15)
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)


async def run_gateway_turn(
    runner: GatewayAgentRunner,
    text: str,
    send: Callable[[str], Awaitable[Any]],
    *,
    logger: Callable[[str], None] = print,
):
    """Run one agent turn, deliver it, and expose failures without secrets."""
    logger(f"you › {text}")
    try:
        result = await runner.respond(text)
    except Exception as exc:
        logger(f"({runner.source}) message handling failed: {safe_exception(exc)}")
        try:
            await send(GATEWAY_ERROR_REPLY)
        except Exception as send_exc:
            detail = runner.record_error(send_exc, action="error reply delivery")
            logger(f"({runner.source}) error reply delivery failed: {detail}")
        return None

    logger(f"waku › {result.reply}")
    try:
        await send(result.reply or "(no reply)")
    except Exception as exc:
        detail = runner.record_error(exc, action="reply delivery")
        logger(f"({runner.source}) reply delivery failed: {detail}")
        return None
    runner.clear_error()
    return result
