"""Every response the gateway writes itself, and the headers on all of them.

THE SENTENCES HERE ARE THE GATEWAY'S OWN. Every sentence a tenant can be shown
about their container comes from group B -- policy.PAUSED_BODY and its block
messages, idle.CAPACITY_MESSAGE, idle.START_TIMEOUT_MESSAGE,
idle.MAINTENANCE_MESSAGE, quota.TURN_LIMIT_MESSAGE. The ones below are about
the REQUEST rather than the container, they exist nowhere else, and none of
them carries a `code`: hosted/core/policy.CODES is a closed set with one
member, pinned against the dashboard's JavaScript by
test_paused_contract.py, and growing it needs a page change in another group.

HARDEN() IS APPLIED TO EVERY RESPONSE THIS PROCESS WRITES, including the ones
it copies from a container. It is a function and not a middleware because a
streaming response is prepared by its own handler and a middleware that runs
after prepare() cannot add a header to it.
"""

from __future__ import annotations

import html
import json

from aiohttp import web

NOT_FOUND = "That is not a page this service serves."
WRONG_HOST = "That host is not served here."
NOT_JSON = "This route takes a JSON body sent from its own page."
NO_SESSION = "Your session has ended. Sign in again."
SERVICE_WORKER = "A service worker cannot be installed here."
TOO_LARGE = "That request is too large."
BAD_REQUEST = "That request could not be read."
REFUSED = "That request was refused."

FRAME_ANCESTORS = "frame-ancestors 'none'"

# Spec, "Responses from a container are not trusted, even on its own origin".
# Every one of these is on EVERY response, apex and tenant host alike: the two
# hosts are same-site, so without nosniff and CORP one tenant's page can embed
# another person's responses with their cookie attached, and without the two
# framing headers it can frame their dashboard. The embedded chat (spec 008) is
# the one page that is framed on purpose; see FRAMABLE below.
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


# Spec 008 D: the ONE exception to the two framing headers, and it is opt-in
# per response. A response marked with this key in its own state (aiohttp's
# StreamResponse is a mapping for exactly this) keeps the frame-ancestors
# allowlist allow_framing wrote and gets no X-Frame-Options. Only the embed
# page and /auth/embed are ever marked; nothing a container sends can mark one,
# because the mark is on the gateway's own response object, not a header.
FRAMABLE = web.ResponseKey("waku_framable", bool)


def allow_framing(response: web.StreamResponse, origins: tuple[str, ...], *,
                  policy: str = "") -> web.StreamResponse:
    """`frame-ancestors <origins>` instead of 'none', and no X-Frame-Options.

    X-Frame-Options has no allowlist form -- ALLOW-FROM was never implemented
    by Chrome and was dropped by Firefox -- so it cannot be narrowed, only
    left off; frame-ancestors is what every current browser reads instead.
    `policy` is the rest of a page's own policy, for a page that has one.
    """
    response[FRAMABLE] = True
    ancestors = "frame-ancestors " + " ".join(origins)
    response.headers["Content-Security-Policy"] = (
        f"{policy}; {ancestors}" if policy else ancestors)
    response.headers.popall("X-Frame-Options", None)
    return response


def harden(response: web.StreamResponse) -> web.StreamResponse:
    """The five headers, applied without clobbering a page's own CSP or a
    response's own Cache-Control.

    /login carries a full policy of its own, which already contains
    frame-ancestors 'none'. Two Content-Security-Policy headers intersect
    rather than override, so sending both would be safe -- but one header with
    one policy is what a person debugging this reads, so the page's policy is
    left alone and the default is added only where there is none.
    """
    for name, value in SECURITY_HEADERS.items():
        # Cache-Control is DEFAULTED, not imposed, for the same reason the
        # policy below is. The default is no-store and every response that
        # says nothing gets it, which is what the spec's acceptance 14
        # requires of a container response. But this function also runs from
        # `on_response_prepare`, on every response, AFTER the handler -- so
        # imposing it silently undid the one place that deliberately sets
        # something else: the apex's public static files, which are allowed
        # to be kept and revalidated (app.py, _cacheable).
        #
        # That is not hypothetical either. The first version of this change
        # set the header in the handler, passed its own unit test, and still
        # served `no-store` on the wire, because this loop ran last.
        if name == "Cache-Control" and name in response.headers:
            continue
        if name == "X-Frame-Options" and response.get(FRAMABLE):
            continue
        response.headers[name] = value
    if "Content-Security-Policy" not in response.headers:
        response.headers["Content-Security-Policy"] = FRAME_ANCESTORS
    return response


def json_error(status: int, message: str) -> web.Response:
    return harden(web.Response(
        status=status, body=json.dumps({"error": message}).encode("utf-8"),
        content_type="application/json"))


def json_ok(payload: dict) -> web.Response:
    return harden(web.Response(body=json.dumps(payload).encode("utf-8"),
                               content_type="application/json"))


# The sign-in page's stylesheets, in its order (tokens first: login.css reads
# them). Both hosts serve /auth/static/ (app.py), so the link is always
# same-origin, which CORP same-origin requires.
ERROR_PAGE_STYLES = ("design/tokens.css", "design/type.css", "design/fonts.css",
                     "design/controls.css", "login.css")


def html_error(status: int, message: str) -> web.Response:
    """A page navigation that fails gets a sentence and a way back, not a
    JSON body the browser renders as text (spec, "Error shape"). It wears the
    sign-in page's card, so it is recognisably Waku rather than bare HTML."""
    safe = html.escape(message)
    links = "".join(f"<link rel=\"stylesheet\" href=\"/auth/static/{name}\">"
                    for name in ERROR_PAGE_STYLES)
    body = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>waku</title>"
            "<link rel=\"icon\" href=\"/auth/static/waku-mark.svg\" type=\"image/svg+xml\">"
            f"{links}</head><body><main>"
            "<img id=\"mark\" src=\"/auth/static/waku-mark.svg\" alt=\"\" width=\"44\" height=\"44\">"
            f"<h1>Waku</h1><p id=\"lede\">{safe}</p><p><a href=\"/\">Try again</a></p>"
            "</main></body></html>")
    return harden(web.Response(status=status, text=body,
                               content_type="text/html", charset="utf-8"))


def redirect(location: str, *, status: int = 302) -> web.Response:
    return harden(web.Response(status=status, headers={"Location": location}))


def wants_html(request: web.Request) -> bool:
    """A page navigation, as opposed to one of the dashboard's fetches."""
    return "text/html" in request.headers.get("Accept", "")


def refusal(request: web.Request, status: int, message: str) -> web.Response:
    return (html_error(status, message) if wants_html(request)
            else json_error(status, message))


TOOK_TOO_LONG = "That took too long. Try again."


# Spec, "Error shape": on a streaming route an error is one terminal `done`
# event carrying `error`, not a JSON body.
#
# THE STATUS CODE IS FOR THE LOG, NOT FOR THE PAGE. waku/ops/static/js/
# render.js calls fetch and goes straight to res.body.getReader(); there is no
# res.ok branch anywhere in the stream consumers. So the page reads this body
# whatever the status, render.js turns {"kind": "done", "error": ...} into
# "Error: <message>" in the dock, and the status is what curl and the access
# log see.
def sse_error(status: int, message: str) -> web.Response:
    frame = json.dumps({"kind": "done", "error": message})
    return harden(web.Response(status=status, text=f"data: {frame}\n\n",
                               content_type="text/event-stream", charset="utf-8"))
