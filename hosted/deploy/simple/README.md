# Static deployment

This deployment runs one Waku container per preallocated account. Caddy asks
for a username and password before forwarding any dashboard request. Each
account has a separate hostname, data directory and Docker network. The
deployment needs Docker Compose and a DNS A record for every hostname. It
uses the existing tenant image and remains under hosted/LICENSE (Elastic
License 2.0).

This deployment does not use Supabase, the spawner, XFS quotas, automatic idle
shutdown or the metering proxy. Containers remain running, and their data can
fill the host disk. Operators manage accounts and backups themselves.

## Build and configure

Build the image from a clean checkout:

```bash
bash hosted/image/build.sh --tenant-only --tenant-tag waku-simple:local
```

Create an accounts JSON file outside the checkout. Each account needs
`username`, `hostname` and `password_hash` (bcrypt). Caddy's `hash-password`
command creates a bcrypt hash; run it interactively rather than passing a
password as a command-line argument. Assign the apex hostname to the first
account and distinct subdomains to subsequent accounts. Do not commit hashes
or passwords.

```bash
docker run --rm -it caddy:2.10.2-alpine caddy hash-password
python3 hosted/deploy/simple/render.py /srv/waku-simple/accounts.json \
  --output /srv/waku-simple
```

For each username, create `/srv/waku-simple/tenants/<username>/home` and
`env`, owned by UID/GID `10001:10001`. Create an empty `env/.env` at mode
0600 and a `home/mcp.json` containing `{"servers": []}`. Preserve existing
files when adding an account. Waku writes its default SOUL and local memory
on startup. Each member configures their own model key in the dashboard.

```bash
cd /srv/waku-simple
docker compose -f compose.json config --quiet
docker compose -f compose.json up -d
```

Caddy obtains individual HTTPS certificates using ports 80 and 443; it does
not require a wildcard certificate or DNS API token. The generated Caddyfile
protects all paths, strips the browser's Authorization header before forwarding
requests, and disables response buffering for streaming chat. Tenant ports
are not published on the host.

If website-port validation fails, obtain a certificate with a DNS challenge
and set `"tls": "manual"` on the relevant account. Install the certificate
chain at `/srv/waku-simple/tls/<username>/fullchain.pem` and its private key
at `privkey.pem` beside it. Keep the key root-owned at mode 0600. Render again
and recreate Caddy with `docker compose up -d caddy`. Only Caddy receives the
certificate directory, mounted read-only; tenant containers do not receive it.
Caddy does not renew manually supplied certificates. Operators must renew
them before their expiry, replace both files and reload Caddy. A manual DNS
challenge needs another DNS update at renewal unless a DNS API automates it.

## Add or disable an account

To add an account, add its DNS record and JSON entry, prepare its directories,
render both configuration files again, then run `docker compose up -d` and
`docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile`.

To disable an account, remove its JSON entry, render again, reload Caddy and
run `docker compose up -d --remove-orphans`. Preserve its data directories.
Browser password prompts do not provide application sessions or a logout
button; users should close the browser session on shared machines. Rotating
an account's bcrypt hash and reloading Caddy invalidates its old password.

## Verify

Confirm that unauthenticated requests to the homepage and `/api/data` return
401 over HTTPS, that valid credentials reach the setup page, and that a
second account's credentials cannot reach the first account's hostname.
Restart a tenant and confirm that its saved conversation is still available.
Use `docker compose ps` and `docker compose logs` to diagnose startup errors.
