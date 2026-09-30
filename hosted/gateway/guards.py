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
# The header, and not a value: see is_service_worker. `script` is the one
# token a browser sends, and it is documented there rather than compared.
SERVICE_WORKER_HEADER = "Service-Worker"
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
# A document navigation, or a browser that sends no Fetch Metadata at all.
# Anything else -- image, iframe, empty (fetch/XHR), script -- is a
# subresource load, and a subresource must not mint a session.
HANDOFF_FETCH_DESTS = frozenset({"", "document"})
FETCH_SITE_HEADER = "Sec-Fetch-Site"
FETCH_DEST_HEADER = "Sec-Fetch-Dest"


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
    that origin's requests after the session that installed it ended.

    THE HEADER'S PRESENCE IS THE REFUSAL, whatever it says. Written as an
    exact comparison against the lowercase token, `Service-Worker: Script`
    was forwarded with a 200 while `script` was refused -- the open-set shape
    again, one function above a guard that already normalises. Case-folding
    that comparison would fix the case at hand and leave the shape: the next
    value nobody listed would be admitted by default. `Service-Worker` is
    defined for exactly one thing, a worker script fetch, and no other client
    has a reason to send it, so nothing about its VALUE is interesting here.
    The one token browsers send is `script`; this refuses that and everything
    else.

    Browsers send the token lowercase, so the original was never a live
    bypass. It is written this way because the failure mode is not
    proportional to the likelihood: a worker installed on a tenant origin
    intercepts every request on that origin for every later session, and this
    guard is the only thing refusing it.
    """
    return SERVICE_WORKER_HEADER in request.headers


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

    ONE HEADER, ONCE. `headers.get` answers the FIRST of a repeated header, so
    a request carrying `Sec-Fetch-Site: same-site` in front of
    `Sec-Fetch-Site: cross-site` would have been served on the first copy. No
    browser sends two, which is the reason to refuse the request rather than
    pick a copy: a duplicate is not a hand-off this gateway issued, whichever
    value it is read from.

    `none` IS REFUSED, DELIBERATELY. It means a user-initiated navigation --
    typed, pasted, or a restored tab. The real hand-off is always the apex
    page's `location.assign`, which is `same-site`, so nothing legitimate
    arrives as `none`; a person who pastes the URL gets a bounce to /login and
    signs in again, and a restored tab would have failed anyway on a code that
    is single-use and sixty seconds old.

    `Sec-Fetch-Dest` IS CHECKED TOO. The hand-off is a document navigation.
    Without it, a page on any waku host -- a tenant's own container serves
    whatever it likes -- could put the enter URL in an <img> or a fetch, which
    is same-site, and mint a session from a subresource load. An absent header
    is admitted for the same reason as an absent Sec-Fetch-Site.
    """
    sites = request.headers.getall(FETCH_SITE_HEADER, ())
    destinations = request.headers.getall(FETCH_DEST_HEADER, ())
    if len(sites) > 1 or len(destinations) > 1:
        return "more than one fetch-metadata header"
    site = (sites[0] if sites else "").strip().lower()
    destination = (destinations[0] if destinations else "").strip().lower()
    if site not in HANDOFF_FETCH_SITES:
        return "cross-site hand-off"
    if destination not in HANDOFF_FETCH_DESTS:
        return "not a navigation"
    return ""
