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
# framing headers it can frame their dashboard.
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def harden(response: web.StreamResponse) -> web.StreamResponse:
    """The five headers, applied without clobbering a page's own CSP.

    /login carries a full policy of its own, which already contains
    frame-ancestors 'none'. Two Content-Security-Policy headers intersect
    rather than override, so sending both would be safe -- but one header with
    one policy is what a person debugging this reads, so the page's policy is
    left alone and the default is added only where there is none.
    """
    for name, value in SECURITY_HEADERS.items():
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


def html_error(status: int, message: str) -> web.Response:
    """A page navigation that fails gets a sentence and a way back, not a
    JSON body the browser renders as text (spec, "Error shape")."""
    safe = html.escape(message)
    body = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width\">"
            "<title>waku</title></head><body>"
            f"<h1>waku</h1><p>{safe}</p><p><a href=\"/\">Try again</a></p>"
            "</body></html>")
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
