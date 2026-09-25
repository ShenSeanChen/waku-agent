"""Only verified, non-anonymous, active users get in -- acceptance 13.

A LOCALLY SIGNED JWKS. Nothing here touches the network: the fixture generates
a P-256 key, publishes it as a JWKS document in the shape the live project's
own document has, and hands the verifier a fetch function that returns it. The
live document was read once, on 2026-09-24, and its shape is what
gatewaylib.signing_key reproduces; a test that fetched it would be a test that
fails on a train.

THE FIRST TESTS IN THIS FILE ARE THE SPEC FINDING. spec.md says the verifier
checks `aud` is "authenticated". A real token from this project carries
aud=https://api.waku.one/mcp and role=authenticated, so a verifier written to
that sentence refuses every real sign-in. They are here so that nobody
"fixes" the audience back to the spec's word without a red test saying what
it costs.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from gatewaylib import (
    AUDIENCE,
    ISSUER,
    JWKS_URL,
    KID,
    Clock,
    cookie_value,
    ops,
    sign,
    signing_key,
)

from hosted.gateway import admin
from hosted.gateway.identity import JwksVerifier, NotSignedIn
from hosted.gateway.launch import DISABLED_MESSAGE
from hosted.gateway.spawner_client import SpawnerError


def verifier_for(jwks: dict, clock: Clock, *, audience: str = AUDIENCE,
                 issuer: str = ISSUER) -> JwksVerifier:
    return JwksVerifier(jwks_url=JWKS_URL, issuer=issuer, audience=audience,
                        fetch=lambda _url: jwks, now=clock)


def test_a_token_shaped_like_this_projects_real_one_verifies():
    """aud is the custom MCP audience and role is `authenticated`. This is the
    decoded shape of a live token, claim for claim."""
    private, jwks = signing_key()
    clock = Clock()
    identity = verifier_for(jwks, clock).verify(sign(private, now=clock.t))
    assert identity.sub == "sub-mei"
    assert identity.email == "mei@example.com"
    assert identity.is_anonymous is False


def test_a_token_whose_aud_is_authenticated_is_refused_by_the_configured_default():
    """The spec's sentence, made a test so it cannot be restored silently.

    `authenticated` is this project's `role`, not its `aud`. A gateway
    configured with the live audience refuses a token that puts the role
    string in the audience -- which is what every Supabase project that has
    NOT customised its audience issues, and admitting those would leave the
    issuer as the only claim doing work.
    """
    private, jwks = signing_key()
    clock = Clock()
    token = sign(private, now=clock.t, aud="authenticated")
    with pytest.raises(NotSignedIn):
        verifier_for(jwks, clock).verify(token)


def test_the_audience_comes_from_configuration():
    """A second deployment, a second audience, no code change."""
    private, jwks = signing_key()
    clock = Clock()
    token = sign(private, now=clock.t, aud="https://other.example/api")
    other = verifier_for(jwks, clock, audience="https://other.example/api")
    assert other.verify(token).sub == "sub-mei"
    with pytest.raises(NotSignedIn):
        verifier_for(jwks, clock).verify(token)


@pytest.mark.parametrize("claims, why", [
    ({"iss": "https://someone-else.supabase.co/auth/v1"}, "a wrong issuer"),
    ({"aud": "authenticated"}, "a wrong audience"),
    ({"role": "anon"}, "the anon role"),
    ({"role": "service_role"}, "the service role"),
    ({"is_anonymous": True}, "an anonymous sign-in"),
    ({"sub": ""}, "no subject"),
])
def test_a_token_carrying_the_wrong_claim_is_refused(claims, why):
    private, jwks = signing_key()
    clock = Clock()
    with pytest.raises(NotSignedIn):
        verifier_for(jwks, clock).verify(sign(private, now=clock.t, **claims))


def test_an_expired_token_is_refused():
    private, jwks = signing_key()
    clock = Clock()
    token = sign(private, now=clock.t)
    clock.t += 601
    with pytest.raises(NotSignedIn):
        verifier_for(jwks, clock).verify(token)


def test_a_token_signed_by_another_key_is_refused():
    private, _mine = signing_key()
    _theirs, jwks = signing_key()
    clock = Clock()
    with pytest.raises(NotSignedIn):
        verifier_for(jwks, clock).verify(sign(private, now=clock.t))


def _hs256(claims: dict, secret: bytes, kid: str) -> str:
    """An HS256 token, assembled by hand.

    By hand because PyJWT 2.13 refuses to ENCODE with a public key or a JWK as
    an HMAC secret -- a forgery built with pyjwt.encode would fail on the
    attacker's own library and prove nothing about this gateway. An attacker
    writes these three segments themselves, so the test does too.
    """
    def segment(obj: dict) -> bytes:
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    signing_input = (segment({"alg": "HS256", "typ": "JWT", "kid": kid})
                     + b"." + segment(claims))
    signature = base64.urlsafe_b64encode(
        hmac.new(secret, signing_input, hashlib.sha256).digest()).rstrip(b"=")
    return (signing_input + b"." + signature).decode("ascii")


def test_an_hmac_token_is_refused_before_the_signing_key_is_looked_up():
    """The oldest JWT confusion: with an HMAC algorithm admitted, the public
    key the JWKS publishes is itself a valid signing secret, because an HMAC
    secret is only bytes.

    WHAT IS OBSERVED IS THE FETCH, not the refusal. A refusal alone cannot
    fail this test -- `algorithms=` would refuse the token a second time
    inside jwt.decode however the header check behaved. The claim in
    identity.py is stronger and is the one measured here: an `alg` outside
    ALGORITHMS never reaches the key lookup, so a stream of tokens naming
    unknown kids with a forged alg buys the attacker no outbound requests at
    all. Delete the header check and the JWKS is fetched once: red.
    """
    private, jwks = signing_key()
    clock = Clock()
    fetches: list[str] = []

    def counted(url: str) -> dict:
        fetches.append(url)
        return jwks

    verifier = JwksVerifier(jwks_url=JWKS_URL, issuer=ISSUER, audience=AUDIENCE,
                            fetch=counted, now=clock)
    published = pyjwt.PyJWK.from_dict(jwks["keys"][0]).key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    forged = _hs256({"iss": ISSUER, "aud": AUDIENCE, "sub": "attacker",
                     "role": "authenticated", "exp": int(clock.t) + 600},
                    published, KID)
    with pytest.raises(NotSignedIn):
        verifier.verify(forged)
    assert fetches == []
    # The same verifier, the same kid, a real ES256 token: one fetch, one
    # identity. So the empty list above is the alg check and not a verifier
    # that refuses everything.
    assert verifier.verify(sign(private, now=clock.t)).sub == "sub-mei"
    assert len(fetches) == 1


def test_an_unknown_kid_refetches_once_and_then_stops():
    """A kid comes from an unauthenticated request, so an unknown one must not
    be an outbound request an attacker chooses the rate of."""
    private, jwks = signing_key()
    clock = Clock()
    fetches: list[str] = []

    def counted(url: str) -> dict:
        fetches.append(url)
        return jwks

    verifier = JwksVerifier(jwks_url=JWKS_URL, issuer=ISSUER, audience=AUDIENCE,
                            fetch=counted, now=clock)
    for _ in range(20):
        with pytest.raises(NotSignedIn):
            verifier.verify(sign(private, now=clock.t, kid="nope"))
    assert len(fetches) == 1
    clock.t += 61
    with pytest.raises(NotSignedIn):
        verifier.verify(sign(private, now=clock.t, kid="nope"))
    assert len(fetches) == 2


def test_a_sign_in_without_json_or_with_a_foreign_origin_is_refused(harness):
    async def run():
        await harness.start()
        token = sign(harness.private, now=harness.clock.t)
        body = json.dumps({"access_token": token}).encode("utf-8")
        wrong_type = await harness.send(
            "POST", "/auth/session", host="agent.waku.one", body=body,
            headers={"Content-Type": "text/plain",
                     "Origin": "https://agent.waku.one"})
        foreign = await harness.json_post(
            "/auth/session", {"access_token": token}, host="agent.waku.one",
            origin="https://evil.example")
        good = await harness.json_post(
            "/auth/session", {"access_token": token}, host="agent.waku.one")
        await harness.stop()
        return wrong_type[0], foreign[0], good[0]

    wrong_type, foreign, good = asyncio.run(run())
    assert (wrong_type, foreign, good) == (415, 415, 200)


def test_a_disabled_tenant_is_refused_at_once_on_both_hosts(harness):
    """Acceptance 13's second half: after the gateway disables a tenant, their
    next request and their next login are refused at once, and the gateway
    issues no token and starts no container for them."""
    async def run():
        await harness.start()
        token = sign(harness.private, now=harness.clock.t)
        status, headers, body = await harness.json_post(
            "/auth/session", {"access_token": token}, host="agent.waku.one")
        assert status == 200
        apex_cookie = cookie_value(headers, "__Host-waku_session")
        enter = json.loads(body)["enter"]
        tenant_id = enter.split("//", 1)[1].split(".", 1)[0]
        code = enter.split("code=", 1)[1]
        host = f"{tenant_id}.agent.waku.one"
        _s, enter_headers, _b = await harness.send(
            "GET", f"/auth/enter?code={code}", host=host)
        tenant_cookie = cookie_value(enter_headers, "__Host-waku_tenant")
        before = await harness.send("GET", "/api/data", host=host,
                                    cookie=f"__Host-waku_tenant={tenant_cookie}")

        # Disabled through the same path tenant.sh uses.
        disabled = await admin.handle(harness.gateway,
                                      {"op": "disable", "tenant": tenant_id})
        assert disabled == {"ok": True, "tenant": tenant_id}

        after = await harness.send("GET", "/api/data", host=host,
                                   cookie=f"__Host-waku_tenant={tenant_cookie}")
        relogin = await harness.json_post(
            "/auth/session", {"access_token": token}, host="agent.waku.one",
            cookie=f"__Host-waku_session={apex_cookie}")
        await harness.stop()
        return tenant_id, before[0], after[0], relogin[0], json.loads(relogin[2])

    tenant_id, before, after, relogin, relogin_body = asyncio.run(run())
    assert before == 200
    assert after == 401
    assert relogin == 403
    assert relogin_body["error"] == DISABLED_MESSAGE
    # One start: the sign-in's pre-warm. Nothing after the disable.
    assert len(ops(harness.spawner, "start")) == 1
    assert ops(harness.spawner, "stop") == [{"op": "stop", "tenant_id": tenant_id}]


def test_a_pre_warm_that_fails_is_not_a_failed_sign_in(harness):
    """The start on /auth/session is a pre-warm, not the sign-in.

    The container boots while the page loads, and E3's forwarder starts it
    again on the first request to the tenant host, so a spawner that refuses
    here must still leave the person signed in with a hand-off code -- and
    with a session row, or the hand-off would land on a host that turns them
    away.
    """
    harness.spawner.fail_start = SpawnerError("no room on the VM")

    async def run():
        await harness.start()
        token = sign(harness.private, now=harness.clock.t)
        status, headers, body = await harness.json_post(
            "/auth/session", {"access_token": token}, host="agent.waku.one")
        enter = json.loads(body)["enter"]
        tenant_id = enter.split("//", 1)[1].split(".", 1)[0]
        host = f"{tenant_id}.agent.waku.one"
        _s, entered, _b = await harness.send(
            "GET", f"/auth/enter?code={enter.split('code=', 1)[1]}", host=host)
        landed = await harness.send(
            "GET", "/api/data", host=host,
            cookie=f"__Host-waku_tenant={cookie_value(entered, '__Host-waku_tenant')}")
        await harness.stop()
        return status, cookie_value(headers, "__Host-waku_session"), landed

    status, apex_cookie, landed = asyncio.run(run())
    assert status == 200
    assert apex_cookie != ""
    assert landed[0] == 200
    assert len(ops(harness.spawner, "start")) == 1
