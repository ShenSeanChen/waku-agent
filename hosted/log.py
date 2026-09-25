"""How hosted/ logs. One convention, decided in group C, used by every service.

FIVE RULES, and the reason for each:

1. STDOUT, not a file. Compose captures stdout and `docker compose logs -f
   spawner` is what an operator has. A log file inside a container is a log
   file nobody finds.

2. configure() IS CALLED BY A SERVICE MAIN, NEVER AT IMPORT. A library that
   calls basicConfig at import steals the root logger from whatever imported
   it, and evals/ imports these modules.

3. ONE LINE PER EVENT, and every line about a tenant carries the tenant id, so
   `grep <id>` is the whole debugging story for one tenant.

4. NEVER A TOKEN, A KEY OR A TENANT'S FILE CONTENTS. A proxy token is the
   password to a tenant's free tier; a tenant's .env may hold their own
   Anthropic key. redact() prints sha256:<8 hex> so two lines can be
   correlated -- "issued sha256:1a2b3c4d" and "started with sha256:1a2b3c4d" --
   without the plaintext ever reaching a log the operator's log shipper may
   send somewhere else. control.db already stores the same hash, so an
   operator can join the two.

5. WHAT THE WIRE HIDES, THE LOG SHOWS. jsonsock answers {"error": "the handler
   failed"} and says nothing more, on purpose: the peer is another service but
   the request that reached it came from a tenant. The operator needs the
   traceback, so the handler logs it here and the wire stays opaque. That is
   the one asymmetry this module exists for.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys

LEVEL_ENV = "WAKU_LOG_LEVEL"
FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


def configure(stream=None) -> None:
    """Called by a service's main(), once. Never at import."""
    logging.basicConfig(
        stream=stream or sys.stdout,
        level=os.environ.get(LEVEL_ENV, "INFO").upper(),
        format=FORMAT,
        datefmt=DATE_FORMAT,
        force=True)


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)


def redact(secret: str) -> str:
    """Enough to correlate two lines, not enough to use.

    The same SHA-256 control.db stores, truncated: an operator who sees
    `started tenant=k3fq7x2mza4b token=sha256:1a2b3c4d` can join it to the
    issue that produced it and to the row in control.db, and cannot replay it.
    """
    if not secret:
        return "sha256:<empty>"
    return "sha256:" + hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8]
