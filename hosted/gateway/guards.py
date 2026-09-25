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

# The two methods that may cross the door without the CSRF pair, named.
# EVERYTHING else needs both halves -- PUT, DELETE, PATCH, OPTIONS, TRACE and
# any verb nobody has thought of. The first draft of this module asked "is it
# a POST?", which is the open set this spec keeps losing to: a PUT with a
# foreign Origin and a text/plain body was forwarded with the tenant's cookie
# on it. Nothing upstream takes a PUT today, which is exactly the kind of
# sentence that stops being true without anybody editing this file.
CHECKED_EXEMPT_METHODS = frozenset({"GET", "HEAD"})

# The hand-off navigation is same-site: the apex and a tenant host share
# agent.waku.one. An absent header is admitted because Fetch Metadata is a
# browser feature, not a wire guarantee -- Safari only began sending it in
# 16.4 -- and a sign-in nobody can complete is worse than the narrow window
# it leaves. Script cannot forge these: Sec-* is a forbidden header name.
HANDOFF_FETCH_SITES = frozenset({"", "same-site", "same-origin"})
FETCH_SITE_HEADER = "Sec-Fetch-Site"


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
    host = raw.strip().lower()
    if host.count(":") > 1:
        return ""
    # The PORT first, THEN the root label's dot. The other order reads
    # "agent.waku.one.:443" as the host "agent.waku.one." and refuses it 421,
    # which is fail-closed but makes this docstring false for a name a
    # resolver treats as ordinary.
    host = host.split(":", 1)[0].rstrip(".")
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

    AN ALLOWLIST OF METHODS, not a test for one method. GET and HEAD are named
    as the two that pass without the pair, because they are what a navigation
    is; every other verb is checked, including the ones this deployment does
    not serve.
    """
    if request.method in CHECKED_EXEMPT_METHODS:
        return ""
    if request.content_type != JSON_CONTENT_TYPE:
        return "not json"
    if request.headers.get("Origin", "") != f"https://{host}":
        return "foreign origin"
    return ""


def handoff_refusal(request: web.Request) -> str:
    """Empty when the hand-off navigation may be honoured.

    GET /auth/enter is the one route on a tenant host served WITHOUT a
    session, so the CSRF pair cannot apply to it -- it is a navigation, and it
    carries no body. Its own risk runs the other way: an attacker who signs in
    as themselves can mint a code and navigate somebody else's browser to
    their tenant host, and that victim then types into the attacker's
    container. `Sec-Fetch-Site` is what tells the two apart: the real hand-off
    is `same-site` (the apex and the tenant host share the registrable
    domain), and a link from anywhere else is `cross-site`.
    """
    site = request.headers.get(FETCH_SITE_HEADER, "").strip().lower()
    return "" if site in HANDOFF_FETCH_SITES else "cross-site hand-off"
