"""Host, service workers and CSRF: the checks that run before anything else.

EVERY ONE OF THEM IS AN ALLOWLIST. A host is served when it is the apex or a
label that is a valid tenant id under the apex, and refused otherwise; an
Origin is accepted when it equals one exact string and refused otherwise. The
alternative -- a list of hosts and origins that are forbidden -- has been
defeated five times on this spec, and it loses the same way each time: the
next shape nobody listed is admitted by default.
"""

from __future__ import annotations

from aiohttp import web

from hosted.core.tenant import is_tenant_id

MISDIRECTED = 421
UNSUPPORTED_MEDIA = 415

JSON_CONTENT_TYPE = "application/json"
SERVICE_WORKER_HEADER = "Service-Worker"
SERVICE_WORKER_VALUE = "script"
HOST_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-.")


def normalise_host(raw: str | None) -> str:
    """The host a request was sent to, lowercased, without its port.

    ONLY THE `Host` HEADER. X-Forwarded-Host is ignored and always will be:
    Caddy sets Host from the request line and the TLS SNI, and a second header
    that could override it is a header a tenant's own page can send.

    Anything that is not a plain ASCII hostname answers "", which every caller
    reads as "not a host this service serves".
    """
    if not raw:
        return ""
    host = raw.strip().lower().rstrip(".")
    if host.count(":") > 1:
        return ""
    host = host.split(":", 1)[0]
    if not host or not host.isascii():
        return ""
    if any(character not in HOST_CHARACTERS for character in host):
        return ""
    return host


def tenant_label(host: str, apex: str) -> str | None:
    """The tenant id in `<id>.<apex>`, or None.

    One label and no more: `a.b.agent.waku.one` is not a tenant host, because
    the wildcard certificate covers one level and a second level is a name
    nobody issued.

    The `"." in label` test is intent, not the guard: `is_tenant_id` already
    refuses anything but twelve characters of [a-z2-7], so no host with a dot
    in its label can reach the second half of that condition. It is written
    out because the next person to widen the tenant alphabet should have to
    delete a line that says what it is for. Nothing can measure it, which is
    said here rather than dressed up as a test.
    """
    suffix = "." + apex
    if not host.endswith(suffix):
        return None
    label = host[: -len(suffix)]
    if "." in label or not is_tenant_id(label):
        return None
    return label


def is_service_worker(request: web.Request) -> bool:
    """Spec: "it refuses any request carrying Service-Worker: script". A
    service worker registered on a tenant's own origin would keep answering
    that origin's requests after the session that installed it ended."""
    return request.headers.get(SERVICE_WORKER_HEADER, "").strip() == SERVICE_WORKER_VALUE


def csrf_refusal(request: web.Request, host: str) -> str:
    """Empty when the POST is fine, the sentence when it is not.

    Spec, "CSRF": every proxied POST and every POST to the gateway's own
    routes must carry Content-Type: application/json and an Origin equal to
    the host it was sent to. Both, not either: SameSite=Lax already stops a
    cross-site POST from carrying the cookie in a modern browser, and these
    two are the layer that does not depend on the browser being modern.

    The scheme is fixed at https because Caddy terminates TLS and a __Host-
    cookie is not sent over anything else.
    """
    if request.method != "POST":
        return ""
    if request.content_type != JSON_CONTENT_TYPE:
        return "not json"
    if request.headers.get("Origin", "") != f"https://{host}":
        return "foreign origin"
    return ""
