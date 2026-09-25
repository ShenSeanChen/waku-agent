# syntax=docker/dockerfile:1.7
# Caddy with exactly one caddy-dns module, named at build time.
#
# The stock caddy image has no DNS provider compiled in, and DNS-01 is the only
# challenge that can issue a WILDCARD certificate -- which is what one origin
# per tenant needs, because a tenant id becomes a DNS label.
#
# DNS_PROVIDER_VERSION IS EMPTY BY DEFAULT AND SHOULD NOT STAY THAT WAY on a
# deployment anybody depends on. With no version, xcaddy resolves the module's
# latest release on the day it builds, so two builds of the same commit produce
# two different Caddys -- and install.sh rebuilds this image on every run.
# install.sh's --dns-module-version passes a Go module suffix, for example
# "@v1.5.0", and logs a NOTE when it is absent.
ARG CADDY_VERSION=2.10.0

FROM caddy:${CADDY_VERSION}-builder AS builder
# Re-declared inside the stage: an ARG before the first FROM is global and is
# not in scope in a build stage until it is named again.
ARG DNS_PROVIDER
ARG DNS_PROVIDER_VERSION=""
RUN xcaddy build --with github.com/caddy-dns/${DNS_PROVIDER}${DNS_PROVIDER_VERSION}

FROM caddy:${CADDY_VERSION}
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
