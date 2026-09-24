# Hosted waku

Hosted waku runs the stock waku dashboard for many people on one Linux VM:
each person gets their own container, their own memory, history, settings and
spend, reached at their own subdomain. The design is
`docs/architecture.md` for local waku; the hosted deployment is described here
and built under this directory.

**This code is not on PyPI.** `pip install waku-agent[hosted]` installs two
libraries (`aiohttp` and `PyJWT[crypto]`) and nothing else: the extra exists so
that a checkout can install what the services need. The hosted code itself
ships in neither the wheel nor the sdist, and the only way to get it is a git
checkout of this repository.

**The free tier is a provider row.** A tenant container runs stock waku with
`WAKU_PROVIDER=waku-platform`, and that row's key and endpoint come from the
container's environment, which points at the metering proxy. The container
never holds a platform key. Adding your own key is the ordinary provider
switch on the Models page.

## Licensing

The code here is MIT, like the rest of the repository. The Waku design system,
the Waku mark and the Waku names are not: they are listed in `LICENSE-BRAND`,
and a hosted deployment serves the stock dashboard, which carries them. **A
third party who wants to offer this as a service under the Waku name needs
AutoManus's written permission.** Nothing in this directory changes that
license; it only describes a deployment of the software the license already
covers.

## What is here

| Directory | Holds |
|---|---|
| `core/` | pure logic: tenant ids, route policy, quota, idle, provisioning, request validation |
| `ports/` | the four replaceable seams and their MVP implementations |
| `gateway/` | the front door: login, sessions, routing by host, `control.db` |
| `proxy/` | the metering proxy in front of the model API, and `ledger.db` |
| `spawner/` | the one process that talks to Docker |
| `image/` | the tenant and services Dockerfiles and their allowlist ignore files |
| `templates/` | the hosted `SOUL.md` and the gateway's own pages |
| `deploy/` | `install.sh`, Compose, Caddy and the operator scripts |

## Running it

Not yet. The install script, the Compose file and the operator guide land in
group F of spec 001; until then this directory holds the offline half.
