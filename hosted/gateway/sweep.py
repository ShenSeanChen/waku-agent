"""The gateway's once-a-minute idle loop: spec 001 task E4, the idle half.

WHAT IT DOES, EVERY SWEEP_SECONDS:

  1. Launcher.stop_idle(): stop every tenant container with nothing in flight
     and no non-background request for the idle window (15 minutes, or
     WAKU_IDLE_MINUTES). Fleet decides; the Launcher stops; one log line per
     stop.
  2. The disk warning: log idle.disk_warning() when the filesystem holding
     /srv/waku passes 80%, the spec's "the idle loop also logs a warning".

WHY IT LIVES IN THE GATEWAY AND NOT THE SPAWNER. Idle is a fact about
requests, and only the gateway sees requests: Fleet's idle clock and in-flight
count are in the gateway's memory. The spawner knows which containers exist
and nothing about whether anybody is using them.

WHY IT MATTERS BEYOND MEMORY. Every start creates a new container from the
current tenant image tag, so an idle stop is also how an upgrade reaches a
tenant: autodeploy runs upgrade.sh without --now, and a tenant who was idle for
the window gets the new image on their next message.

A SWEEP THAT RAISES DOES NOT END THE LOOP. It is logged, and the next sweep
runs a minute later. A loop that died on its first spawner timeout would stop
nobody ever again, with one line in the log a day earlier to say so.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

from hosted import log
from hosted.core import idle
from hosted.gateway.launch import Launcher

_LOG = log.get(__name__)


class IdleLoop:
    def __init__(self, launcher: Launcher, *, disk_path: Path,
                 interval: float = idle.SWEEP_SECONDS,
                 disk_usage: Callable[[Path], tuple] = shutil.disk_usage,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._launcher = launcher
        self._disk_path = disk_path
        self._interval = interval
        self._disk_usage = disk_usage
        self._sleep = sleep
        # The last disk warning written, so a disk sitting at 85% is said once
        # when it gets there and again only when the number moves, rather than
        # 1,440 times a day.
        self._last_disk_warning = ""

    async def sweep(self) -> list[str]:
        """One pass. Returns the tenant ids it stopped."""
        stopped = await self._launcher.stop_idle()
        self._check_disk()
        return stopped

    def _check_disk(self) -> None:
        try:
            usage = self._disk_usage(self._disk_path)
        except OSError as exc:
            _LOG.warning("could not read disk usage of %s: %s", self._disk_path, exc)
            return
        warning = idle.disk_warning(usage[1], usage[0])
        if warning and warning != self._last_disk_warning:
            _LOG.warning("%s", warning)
        self._last_disk_warning = warning

    async def run(self) -> None:
        """Sweep every interval until cancelled."""
        while True:
            await self._sleep(self._interval)
            try:
                await self.sweep()
            except Exception:  # noqa: BLE001 -- see the module docstring
                _LOG.exception("idle sweep failed; the next one runs in %ss",
                               self._interval)
