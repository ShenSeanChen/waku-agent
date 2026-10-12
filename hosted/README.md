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

**The code here is NOT Apache-2.0.** `hosted/` is licensed under the Elastic
License 2.0 — see [LICENSE](LICENSE). The rest of the repository is Apache-2.0
([../LICENSE](../LICENSE)), including all of `waku/`, which is what PyPI ships.

What that means in practice: **you may run this yourself, including for your
own company, and you may read and modify it. You may not offer it to third
parties as a hosted or managed service.** That one sentence is the whole
difference.

`hosted/` belongs to AutoManus Technologies, Inc., as do the Waku design
system, the Waku mark and the Waku names — those are covered separately, listed in
[LICENSE-BRAND](../LICENSE-BRAND), and a deployment serves the stock dashboard,
which carries them. Neither license grants any right in them.

## What is here

| Directory | Holds |
|---|---|
| `core/` | pure logic: tenant ids, route policy, quota, idle, provisioning, request validation |
| `ports/` | the four replaceable seams, as Protocols and nothing else. The implementations live beside the service that owns each one |
| `gateway/` | the front door: login, sessions, routing by host, `control.db` |
| `proxy/` | the metering proxy in front of the model API, and `ledger.db` |
| `spawner/` | the one process that talks to Docker |
| `image/` | the tenant and services Dockerfiles, their allowlist ignore files, and the seccomp profile every container runs under |
| `templates/` | the hosted `SOUL.md` and the gateway's own pages |
| `deploy/` | `install.sh`, Compose, Caddy, the operator scripts, and the example env files they write |

## Running it

Hosted waku runs on **one Ubuntu 24.04 VM**. Everything below is done once, in
this order. The example domain here is the deployment this was built for,
`agent.waku.one`; substitute your own.

### 1. What to create at your provider, by hand

Spec 001 does not automate this layer (design section 11), and `install.sh`
refuses to run until it is right.

| Create | Notes |
|---|---|
| A VM | 4 vCPU, 16 GB RAM to start. Memory is the binding resource: about 100 MB per active tenant, and `install.sh` sizes its running cap from it as (memory minus 2 GB) divided by 150 MB, which is 95 on a 16 GB VM. Pass `--max-running N` to choose your own number. Idle containers stop on their own after 15 minutes (`WAKU_IDLE_MINUTES` in `config/gateway.env`), so the cap limits how many tenants run at once and not how many can sign up. At the cap, the next tenant's start stops the container idle longest |
| A second disk | 100 GB. It becomes `/srv/waku` |
| **No instance role, and no service account** | The tenant firewall rules are the first line, and this is the second: if a rule is ever missing, the metadata service must have no credential to hand out |
| Two DNS records | `agent.waku.one` and `*.agent.waku.one`, both pointing at the VM. Each tenant gets their own host, so the wildcard is not optional |
| A DNS API token | Caddy answers the DNS-01 challenge with it. It is the one credential on the VM that can change your DNS; it lives in `config/caddy.env`, root-only |
| An S3-compatible bucket | For restic. S3, GCS, R2 or B2 |
| A Supabase project | Magic-link sign-in. It must use **asymmetric signing keys** (ES256 or RS256) and it must have **signup turned off**: this deployment is invite-only, and you invite people from the Supabase dashboard |

### 2. The data disk, XFS with project quotas

Every tenant's 1 GB limit is an XFS project quota, so this is not optional
either.

```bash
sudo mkfs.xfs -q /dev/nvme1n1
sudo mkdir -p /srv/waku
# BY UUID, NOT BY DEVICE NODE. Several providers renumber NVMe devices across a
# reboot, and /etc/fstab naming a node that moved is a VM that boots with
# /srv/waku missing and every tenant's data unreachable.
echo "UUID=$(sudo blkid -s UUID -o value /dev/nvme1n1) /srv/waku xfs defaults,prjquota 0 2" \
  | sudo tee -a /etc/fstab
sudo mount /srv/waku
# Mounted is not enforcing. A filesystem without prjquota accepts every
# xfs_quota command and enforces none of them.
sudo xfs_quota -x -c 'state -p' /srv/waku | grep 'Enforcement: ON'
```

`--data-device` still takes the device node, `/dev/nvme1n1`: the spawner runs
`xfs_quota` against the block device, not against a mount point.

### 3. The checkout

**The hosted code is not on PyPI.** `pip install waku-agent[hosted]` installs
two libraries and nothing else; the services are built from a git checkout.

```bash
sudo git clone https://github.com/ShenSeanChen/waku-agent /srv/waku/src
sudo git -C /srv/waku/src checkout main
```

**Pick the ref deliberately.** A deployment sits on whatever commit is checked
out here, and `upgrade.sh` moves it: with no `--ref` it fetches `origin/main`,
and `upgrade.sh --ref v0.4.0` pins a tag. With automatic upgrades on, a timer
moves it instead; see "Automatic upgrades" below. Installing from `main` means
installing whatever landed today; installing from a tag means choosing when to
move. Either is fine, and the one that is not fine is not knowing which you
did. `install.sh` records the commit it built from in
`/srv/waku/config/install.env` as `WAKU_INSTALLED_COMMIT`.

### 4. DNS, TLS, and the Caddy you may already be running

The two records from step 1 both point at the VM:

```
agent.waku.one       A    203.0.113.10
*.agent.waku.one     A    203.0.113.10
```

**Caddy gets the certificate through DNS-01, not HTTP-01.** A wildcard
certificate cannot be issued any other way, so Caddy writes a TXT record in
your zone and needs your DNS provider's API token to do it. `install.sh`
rebuilds Caddy as a container from source with the
[caddy-dns](https://github.com/caddy-dns) module you name in
`--dns-provider`: `route53`, `cloudflare`, `digitalocean` or whichever module
matches your provider, spelled as it appears under that organisation. A module
whose Caddy directive takes an inline argument passes the whole directive:
`--dns-provider 'cloudflare {env.CLOUDFLARE_API_TOKEN}'`, with the token
itself in the file from step 5. Pin the module with
`--dns-module-version @v1.6.2` if you want a rebuild next month to produce the
same Caddy.

**Stop and disable any Caddy you are already running, before you run
`install.sh`.** The container binds ports 80 and 443 on the host, so the two
cannot coexist. `install.sh` does not stop it for you: stopping a service you
built yourself is not an installer's business, and it would take your holding
page down without asking. The moment you run these two commands is the moment
that page stops serving.

```bash
sudo systemctl stop caddy
sudo systemctl disable caddy
```

`install.sh` refuses, before it changes anything, if either port is still held
by something that is not this deployment's own Caddy container, and its
refusal names the port, the process and the container. That is the first check
it runs, ahead of the apt install and both image builds, so a contested port
costs a message and nothing else.

### 5. The three credential files

`install.sh` takes three secrets as **files**, never as values on the command
line. A value typed as an argument lands in root's shell history and in `ps`
output for the whole install, and the installer can clean up neither.

| File | Flag | What happens to it |
|---|---|---|
| The platform's model key | `--platform-key-file` | Only with `--free-model`. Copied into `/srv/waku/config/proxy.env` at mode 0600. Delete the source file afterwards |
| The DNS provider's API token, as `NAME=VALUE` lines | `--dns-env-file` | Copied into `/srv/waku/config/caddy.env` at mode 0600. Delete the source file afterwards |
| The restic repository's password | `--restic-password-file` | **Not copied.** `config/backup.env` names the path, and restic opens the file every night |

Write all three before you install. `install.sh` reads each one as part of its
argument checks and refuses a file that is missing, unreadable, empty, or that
holds anything but the value: one line, no spaces, no blank line before it, and
no carriage return.

```bash
sudo mkdir -p -m 0700 /srv/waku/config

# The platform's model key: the value on one line and nothing else.
sudo sh -c 'umask 077; printf %s "sk-ant-..." >/root/platform-key'

# The DNS provider's credentials, as the caddy-dns module reads them from the
# environment. These are route53's names; deploy/caddy.env.example carries them
# and the cloudflare shape beside them.
sudo sh -c 'umask 077; cat >/root/route53-credentials' <<'EOF'
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
EOF

# The restic repository's password. Generated rather than chosen: nothing ever
# types it.
sudo sh -c 'umask 077; head -c 32 /dev/urandom | base64 >/srv/waku/config/restic-password'
```

**Which variables the DNS file needs depends on your module.** Each caddy-dns
module reads its own; `route53` takes the two AWS names above, `cloudflare`
takes `CLOUDFLARE_API_TOKEN`, and
[deploy/caddy.env.example](deploy/caddy.env.example) shows both shapes. Look up
your module under [caddy-dns](https://github.com/caddy-dns) for the rest.

`--dns-env NAME=VALUE` exists for the **non-secret** variables a module needs,
such as `AWS_REGION`, and is repeatable. Do not put a token there: it stays in
root's shell history and in `ps` output for the whole install.

**Keep a copy of that password somewhere that is not this VM.** It is the only
one of the three that cannot be recovered from the VM's own config, and it is
the one that matters most: a repository whose password is lost is a repository
nobody can read, including you. Losing it turns every nightly backup you have
ever taken into bytes nobody can open. Put it where you would put a root
password.

**Keep the other two, and the install command line, in the same place.** If the
VM is gone you need these to rebuild: the restic password, the DNS token, the
flags you installed with, and the platform model key if you configured a free
tier at all (most deployments should not -- see "What is not enabled yet"). Step 7 below tells you to
delete the two credential files from the VM once they have been copied into
`config/`, which is right -- they are copies of a secret sitting in `/root`. It
does not mean destroy the only copy you have. Paste the whole `install.sh`
command line into the same password-manager entry: `--supabase-audience`,
`--acme-email`, `--free-model`, `--max-running` and `--tenant-disk` are printed
only by `migrate.sh --out`, which you cannot run on a machine that has died, and
`--tenant-disk` silently defaults to 1G and re-quotas every restored tenant.

### 6. Choosing the restic repository

`--restic-repository` has no default, and picking it is a real decision rather
than a formality. The two answers cover different failures.

- **A local path**, for example `/srv/backups/waku`, protects against a bad
  restore and against a tenant destroying their own data. On a separate disk it
  also survives the data volume failing. It does not protect against losing the
  instance: the repository dies with the VM.
- **An object store**, for example `s3:s3.amazonaws.com/waku-backups`, protects
  against losing the instance, the disk and the region. It costs a network
  round trip per snapshot and it needs its own credentials.

Pick the object store unless you have a reason not to. `install.sh` has no flag
for the object store's own credentials: add `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY` to `/srv/waku/config/backup.env` by hand after the
install, following [deploy/backup.env.example](deploy/backup.env.example). A
rerun never overwrites that file, so what you add survives.

**Single-quote both keys.** `backup.env` is sourced as shell, by root, inside the
backup timer at 03:17: a key holding `$` is silently truncated to the part before
it, and one holding a backtick or `$(...)` is a command substitution performed as
root. `AWS_SECRET_ACCESS_KEY='...'` is safe whatever the provider minted.

### 7. Install

```bash
sudo /srv/waku/src/hosted/deploy/install.sh agent.waku.one \
  --dns-provider route53 \
  --dns-env-file /root/route53-credentials \
  --dns-env AWS_REGION=us-east-1 \
  --acme-email ops@example.com \
  --data-device /dev/nvme1n1 \
  --supabase-url https://<project>.supabase.co \
  --supabase-publishable-key sb_publishable_... \
  --supabase-audience <the aud claim your project's tokens carry> \
  --restic-repository s3:s3.amazonaws.com/<bucket> \
  --restic-password-file /srv/waku/config/restic-password
```

No `--free-model` and no `--platform-key-file`: this install offers no free
tier, and tenants bring their own key. To offer one, add
`--free-model claude-sonnet-5-5 --free-small-model claude-haiku-4-5
--platform-key-file <file>`; "The free tier" below says what that runs.

It refuses, with a readable message, when the VM is not Ubuntu 24.04, when
`/srv/waku` is not XFS mounted with `prjquota`, when either of ports 80 and 443
is held by something that is not this deployment's own Caddy, when the Supabase
project signs with a shared secret, and when the project has open signup. It
reads the mount OPTION and not the enforcement state, which is why step 2 runs
`xfs_quota -x -c 'state -p'` itself.

It is **idempotent and never overwrites a config file**. A rerun says which
files it kept. To change a value, edit the file under `/srv/waku/config/` and
restart that service.

When it finishes, the apex is `https://agent.waku.one` and each tenant is
`https://<id>.agent.waku.one`. Caddy asks for the certificate on the first
request to the apex, and that request can take a minute while the DNS-01
challenge propagates.

Then do three things it deliberately leaves to you:

```bash
# 1. The object store's own credentials, appended by hand (step 6).
sudo nano /srv/waku/config/backup.env

# 2. Create the restic repository. NOTHING ELSE DOES: install.sh cannot,
#    because the credentials above are not there while it runs, and the
#    nightly run refuses rather than creating what it cannot open.
sudo /srv/waku/src/hosted/deploy/backup.sh --init-repository

# 3. Delete the two credential files you passed in. Both values are now in
#    /srv/waku/config/, mode 0600, in a directory only root can enter.
sudo rm /root/platform-key /root/route53-credentials
```

### 8. Invite somebody

Signup is off, so people arrive by invitation: invite an email address from the
Supabase dashboard. They open the magic link, land on `agent.waku.one/login`,
and finish at `https://<their id>.agent.waku.one`.

The link opens in a new tab. If the tab they asked for it from is still open in
the same browser, that tab signs in instead, and the new tab says they can
close it: the two talk over a same-origin `BroadcastChannel` in
`hosted/gateway/static/login.js`, because the enter code works once. In another
browser, or with no asking tab open, the new tab signs in as before.

## Each person's Waku Memory

Every tenant's agent reaches that person's own Waku Memory without a browser
sign-in inside the container (spec 004):

1. At the person's first sign-in, the gateway calls Waku Memory's `POST /keys`
   with the person's own Supabase token and the label `Waku Agent (hosted)`.
   The API base is `WAKU_SUPABASE_AUDIENCE` without `/mcp`. A failure is logged
   and the sign-in goes ahead; the next sign-in tries again.
2. The key is stored in `control.db`, table `memory_key`, and every container
   start passes it in as `WAKU_MEMORY_API_KEY`.
3. Provisioning adds a `waku_memory` server to the tenant's `/data/mcp.json`
   with `"auth_env": "WAKU_MEMORY_API_KEY"`. A tenant's own servers are kept.
4. Provisioning writes `WAKU_CONSOLIDATE_EVERY=1` into the tenant's `.env`, so
   every turn ends with consolidation, and the agent sends each fact it keeps
   to Waku Memory over that server (spec 006). The client names itself
   `waku-agent`, which Waku Memory labels as Origin `waku`.
   Company research goes to the project `Company brain`; everything else goes
   to scope `global`. A failed send is retried at the next turn.

Provisioning creates `.env` only when it is missing. A tenant provisioned
before spec 006 keeps the laptop default of 6 exchanges until
`WAKU_CONSOLIDATE_EVERY=1` is added by hand to their `.env`, which is
`/work/.env` inside the container.

The key is the person's own: it reaches only their memory, and they can revoke
it on waku.one under Settings, API keys. At most once an hour, a sign-in or a
chat call asks Waku Memory whether the stored key is still live; a revoked one
is replaced and the container restarted onto the new key. An unreachable Waku
Memory changes nothing.

## The chat API other surfaces call

`POST https://agent.waku.one/v1/chat` sends one message to a person's own Waku
Agent (spec 004). The waku.one Waku Agent tab calls it, and Slack will. It is
called server to server:

```bash
curl -N https://agent.waku.one/v1/chat \
  -H "Authorization: Bearer <the person's Supabase access token>" \
  -H "Content-Type: application/json" \
  -d '{"message": "what did my competitors ship this week?"}'
```

The gateway verifies the token exactly as it does at sign-in, finds or creates
that person's tenant, mints their Waku Memory key if they have none, and relays
the container's own `/api/chat/stream`: Server-Sent Events, ending in a frame
with `"kind": "done"`. The turn counts against the same hourly quota as a turn
typed into the dashboard. The container never sees the token. The route ignores
cookies, so it does not check `Origin`; it still requires a JSON body.

### Chat history

Three more routes take the same bearer token and go through the same steps:
the token verified, the tenant found or created, the Waku Memory key minted if
missing, the container woken like a turn, the token never forwarded (spec
007 D). The history stays in the container's own `chat_log`, one copy; the
gateway reads it through the dashboard's `/api/session`. None of them counts
as a turn.

| Route | The container sees | Answers |
|---|---|---|
| `GET /v1/conversations` | `GET /api/session?action=list` | `{"ok": true, "current": "<id>", "conversations": [{"id", "title", "last_at", "count"}]}`, newest first |
| `GET /v1/conversations/<id>` | `GET /api/session?action=history&id=<id>` | `{"ok": true, "session_id": "<id>", "history": [{"role", "content", "meta"}]}`, oldest first |
| `POST /v1/conversations` | `POST /api/session`, the body as sent | `{"ok": true, "session_id": "<id>", "history": [...]}` |

The POST body is `{"action": "new"}` or `{"action": "switch", "id": "<id>"}`;
anything else is refused with 400 before it reaches the container. `title` is
the conversation's first message, `last_at` its newest row in UTC
(`YYYY-MM-DD HH:MM:SS`), `count` its rows, both sides. `current` is the
conversation the next `POST /v1/chat` writes to; `new` and `switch` change it.
An id is letters, digits, `.`, `_`, `:` and `-`; another shape is a 404.
Refusals are the gateway's own `{"error": "<sentence>"}`, as on `/v1/chat`.

```bash
curl https://agent.waku.one/v1/conversations \
  -H "Authorization: Bearer <the person's Supabase access token>"
```

### The agent's own chat, in a frame

waku.one shows the person's own chat, the dashboard's chat column and nothing
else of the dashboard, in an iframe (spec 008). Its server asks for a one-time
URL with the same bearer token and the same steps as `/v1/chat`:

```bash
curl https://agent.waku.one/v1/embed -X POST \
  -H "Authorization: Bearer <the person's Supabase access token>" \
  -H "Content-Type: application/json" -d '{}'
# {"url": "https://<tenant>.agent.waku.one/auth/embed?code=<code>", "expires_at": 1791043260}
```

The page puts `url` in the iframe. The code is random, valid 60 seconds, works
once and only on that tenant's host; it is held in the gateway's memory, so a
restart costs waku.one one more call. `GET /auth/embed` must arrive as an
iframe navigation (`Sec-Fetch-Dest: iframe`, or no fetch metadata); it sets
`__Host-waku_embed` (`Secure; HttpOnly; SameSite=None; Partitioned`, 12 hours,
stored in `control.db` like the other two sessions and ended by the same
sign-out) and redirects to `/embed/chat`. waku.one may add `&theme=light` or
`&theme=dark` to the URL before framing it; the redirect then goes to
`/embed/chat?theme=<it>`, and the page is served with that `data-theme` on
`<html>`, so its first paint is in the console's theme rather than a light
flash. Any other value, or none, redirects to `/embed/chat` alone and the chat
uses its own stored choice.

A request carrying only that cookie reaches `/embed/chat`, `/static/`,
`POST /api/chat/stream`, `GET` and `POST /api/session` (the chat's header reads
`GET /api/session?action=state`), and `POST /api/providers` with a body that
names a model and nothing else, which is the header's model picker. Everything
else answers 403 inside the frame. The dashboard's own cookie is unaffected.

A browser tab at the tenant host gets the frame's cookie too: the cookie is
partitioned by top-level site, and a frame on `dev.waku.one` and a tab at
`<tenant>.agent.waku.one` share the site `waku.one`. So on a top-level page
(`Sec-Fetch-Dest: document`, or no fetch metadata and an HTML `Accept`) the
gateway ignores the embed cookie for anything but the chat and redirects to
`https://agent.waku.one/login`, as for anyone signed out. The 403 stays for
requests from inside the frame, where a 401 would read as a session that ended.

The chat's own "Dashboard" button opens the full dashboard signed in. It posts
to `/auth/dashboard` with the session it has (the JSON-and-Origin pair applies),
which answers `{"url": "/auth/enter?code=<code>"}`: a sign-in hand-off code from
the same set the apex's sign-in uses (60 seconds, once, this tenant only), and
the button opens it in a new tab. The full session is still made only by
`/auth/enter`, as a top-level navigation that is not cross-site. A plain link
to `https://<tenant>.agent.waku.one/` from waku.one lands on the sign-in page.

Only `/embed/chat` and `/auth/embed` may be framed, and only by the origins in
`WAKU_EMBED_ORIGINS`: they carry `Content-Security-Policy: frame-ancestors
<origins>` and no `X-Frame-Options`; every other response keeps `DENY` and
`frame-ancestors 'none'`. The name is optional in `config/gateway.env`; absent
or empty it is `https://www.waku.one https://waku.one https://dev.waku.one`.
To develop waku.one locally against this gateway, write the whole list with
`http://localhost:3000` added and run `upgrade.sh`. A value that is not an
origin (a path, a wildcard, a quote) stops the gateway at startup.

When there is no session to show, both paths answer a short framable page
instead of the sign-in page, which cannot be framed. Inside the frame the chat
tells the page around it with `postMessage`, to the framing page's origin and
only when that origin is on the allowlist, never `*`:

| When | Message |
|---|---|
| a turn ends | `{"source": "waku-agent", "type": "turn-done", "credits_changed": true}` |
| a report is saved | `{"source": "waku-agent", "type": "report-saved", "memory_id": "...", "title": "..."}` |
| the session has ended | `{"source": "waku-agent", "type": "session-expired"}` |
| the person clicks "Open report" | `{"source": "waku-agent", "type": "open-report", "memory_id": "...", "title": "..."}` |

The chat listens for three messages from the page around it:

| When | Message (to the frame) |
|---|---|
| the person clicks waku.one's "New chat" | `{"source": "waku-console", "type": "new-chat"}` |
| the frame loads, and whenever waku.one's theme changes | `{"source": "waku-console", "type": "theme", "theme": "light"}` or `"dark"` |
| the person presses "Ask Waku" on the bird's brief card | `{"source": "waku-console", "type": "ask", "prompt": "brief-new", "since": "<ISO-8601 time>"}` |

Each is accepted only when `event.source` is `window.parent` and `event.origin`
is on the same allowlist (never `*`, never the frame's own origin). `new-chat`
starts a new chat exactly as "+ New chat" does, and does nothing when the chat
is already empty (waku-memory spec 040 P2). `theme` restyles the chat at once;
its value must be exactly `light` or `dark` (waku.one resolves "system"
itself). It is applied and never stored: the frame shares `localStorage` with
the person's own dashboard on the same host, and the console's theme must not
replace the choice they made there with the dashboard's toggle (waku-memory
spec 040 T). `ask` starts a new chat and sends one sentence as if the person
typed it, but the console never sends words: `prompt` is an id the frame maps
to its own fixed sentence ("Brief me on what's new in my Waku Memory since
<time>."), and `since` must be a zoned ISO-8601 time within the last 90 days,
which the frame parses and writes back itself (waku-memory spec 040 V). An
unknown id or a bad time is ignored, so nothing that can post as waku.one can
put free text in the person's mouth. Every other message is ignored.

"Open report" opens no tab while framed: waku.one opens the report in place
(waku-memory spec 040 M3). Not framed, or framed by an origin off the list, it
opens `/memories/<id>` in a new tab as before.

The framing origin is read from `document.referrer`, so the waku.one page must
not send `Referrer-Policy: no-referrer`; without a referrer nothing is posted.

## The free tier

With `--free-model` and `--platform-key-file`, every tenant can use Waku
without a key of their own (spec 004 C, which builds spec 001's group D). Their
`waku-platform` provider points at the metering proxy on `10.88.0.1:8788`,
which holds the platform key and nothing else does:

- **Models.** Sonnet 5.5 for turns, Haiku 4.5 for the retrieval gate and
  consolidation (`--free-model`, `--free-small-model`). Both are on the
  allowlist; any other model is refused.
- **The cap.** Credits are the limit (below). $1 a month per person, at list
  prices, applies only when the person's balance cannot be read: no Waku
  Memory key yet, or Waku Memory unreachable
  (`hosted/proxy/prices.py`). Each call reserves its worst case first, from
  Anthropic's `count_tokens`, and is settled from the usage Anthropic reports.
  A person at their dollar never reaches Anthropic.
- **The limits.** 4 calls at once and 60 a minute per person, 16 at once
  across the VM, `max_tokens` at most 8192.
- **What it refuses.** Images, documents, server tools, `cache_control` and any
  body field outside the allowlist. Thinking blocks Sonnet returns are accepted
  back, because Waku sends them on the next call.

- **One wallet.** Each settled call is also charged to the person's waku.one
  credits, at 25,000 credits a dollar, with their own Waku Memory key (spec
  004 D; `POST /agent-usage`). A Free person with no credits left is refused
  before their call reaches Anthropic. When Waku Memory cannot be reached, the
  $1 cap is the only limit.
- **What a turn cost.** Each model call from a container carries
  `X-Waku-Turn: <turn_id>`, which the proxy reads and never forwards to
  Anthropic. For one hour, in memory, the proxy keeps each turn's settled
  dollars, its call count, and the credits Waku Memory answered as `charged`
  for those calls and for the treg calls the relay charged during the turn.
  `GET /v1/turns/<turn_id>/charges`, with the container's own platform token,
  answers `{"model_usd": 0.0712, "calls": 5, "credits": 2400}`; another
  tenant's turn and a turn the proxy never saw answer 404. `credits` is null
  while a charge is pending or after one failed. The chat's receipt line
  shows these numbers (waku-agent spec 011; `hosted/proxy/turns.py`).

`config/proxy.env` holds all of it. `ledger.db` holds each person's spend.

### Turning it on for a deployment installed without it

`upgrade.sh` never writes `config/`, so a deployment installed with no free
tier gets one by hand. With the platform key in a root-only file:

```bash
sudo install -m 0600 /dev/null /srv/waku/config/proxy.env
sudo bash -c '. /srv/waku/src/hosted/deploy/envfiles.sh; root=/srv/waku; \
  platform_key=$(cat /root/platform-key); free_model=claude-sonnet-5-5; \
  free_small_model=claude-haiku-4-5; waku_proxy_env > /srv/waku/config/proxy.env'
sudo bash -c 'printf "%s\n" WAKU_PLATFORM_BASE_URL=http://10.88.0.1:8788 \
  WAKU_PLATFORM_MODEL=claude-sonnet-5-5 WAKU_PLATFORM_SMALL_MODEL=claude-haiku-4-5 \
  >> /srv/waku/config/spawner.env'
sudo /srv/waku/src/hosted/deploy/upgrade.sh --now
sudo shred -u /root/platform-key
```

`upgrade.sh` starts the proxy because `hosted/proxy/__main__.py` is now in the
checkout, and `--now` restarts every tenant onto the free tier. Check it from a
tenant's Models page: "Hosted free tier" is enabled and current, and a message
gets an answer.

A tenant's own key, once added, is not counted: it is theirs.

## treg in every container

With a treg token in `config/proxy.env`, every tenant's agent can search and
call treg's catalog (SEO, social, enrichment, scraping, generation) and pays
for it in waku.one credits (spec 004 E). The token never enters a container:
a tenant can read its own environment, and the token spends the platform's
treg balance. So each tenant's `mcp.json` gets a `treg` server pointing at the
metering proxy, `http://10.88.0.1:8788/treg/mcp/`, authenticated with the
container's own platform token, and the proxy (`hosted/proxy/treg.py`):

- **finds the tenant** from that token, as it does for model calls. Unknown or
  disabled is a 401.
- **refuses who cannot pay.** treg is charged to the person's waku.one credits
  with their own Waku Memory key, so a tenant with no key yet is refused, and
  so is a Free person at zero credits. When Waku Memory cannot be reached, the
  call goes ahead and the two fences below still hold.
- **forwards to `https://treg.to/mcp/v2/`**, treg's catalog-only surface, with
  the platform token, `X-Treg-Meta: customer=<tenant id>` and the per-call
  ceiling. Not `/mcp/`: that surface also calls the treg team's OWN connected
  accounts with their credentials injected, which no tenant may reach. Only
  the catalog tools are allowed through; `balance` and `resources_list`,
  which read the platform team's own account, are refused.
- **caps each call** at `WAKU_TREG_MAX_CALL_USD` (default $0.50), written into
  every catalog call as `X-Treg-Route-Max-Cost`. treg answers 402 above it and
  charges nothing.
- **charges what treg reports.** Each call's `cost_usd` is charged in credits
  at the same rate as model calls, under treg's own call id, so a retried
  report never bills twice. A team's free endpoint carries no cost and charges
  nothing; treg bills nothing on a 4xx or 5xx, and neither does the proxy.

Off by default: `install.sh` writes `WAKU_TREG_TOKEN=` empty, and a deployment
installed before spec 004 E has no such line at all. Either way the relay's
route answers 404, and no container gets a `treg` entry.

### Turning it on

Two files, then an upgrade. `upgrade.sh` never writes `config/`, so the lines
go in by hand. The token is the treg team's API key (treg dashboard, Getting
started, Your API key). These lines read it without echoing it, and
`printf` is a shell builtin, so it never appears in the process list or in
your shell history:

```bash
printf 'treg token: '; read -rs treg_token; echo
[ -n "$treg_token" ] || echo "no token read; run the line above again"
printf 'WAKU_TREG_TOKEN=%s\nWAKU_TREG_MAX_CALL_USD=0.50\n' "$treg_token" | sudo sh -c '
  set -e; f=/srv/waku/config/proxy.env; [ -f "$f" ]
  umask 077; t=$(mktemp "$f.XXXXXX")
  grep -v -e "^WAKU_TREG_TOKEN=" -e "^WAKU_TREG_MAX_CALL_USD=" "$f" > "$t" || [ $? -eq 1 ]
  cat >> "$t"; chmod 600 "$t"; mv "$t" "$f"'
unset treg_token
sudo sh -c 'f=/srv/waku/config/spawner.env; grep -q "^WAKU_TREG_RELAY=on$" "$f" \
  || echo WAKU_TREG_RELAY=on >> "$f"'
sudo /srv/waku/src/hosted/deploy/upgrade.sh --now
```

The first file is the proxy's: the relay is on once the token is there. The
second tells the spawner, which cannot read `proxy.env` (it also holds the
model key), to add `treg` to each tenant's `mcp.json`; the spawner refuses
`WAKU_TREG_RELAY=on` without the free tier. Running it again replaces the two
proxy lines rather than adding a second pair. Compose recreates the proxy and
the spawner when their env files change, and `--now` restarts every running
tenant, which provisions the new entry. A tenant who already has a `treg`
server of their own keeps it.

### A tenant's own treg key

A person who has a treg team of their own can use it instead (spec 014). On
the Connections page the treg card reads "Through waku.one: paid in your Waku
credits." and has a Configure button. They paste their org-scoped treg key,
Test connection asks treg (`GET https://treg.to/tools`, free) whether it is
good, and Save writes `TREG_API_KEY` to their own `.env`, like a Tavily key.
The card then reads "Your own treg key: paid by your treg account."

From the next message their agent reaches treg at `https://treg.to/mcp/` with
that key and never touches the relay, so waku.one charges nothing: the same
rule as a tenant's own model key. It is treg's team surface, not `/mcp/v2/`:
the team is theirs, so its connected accounts, its own tools, `balance` and
`resources_list` are theirs to use, and the prompt stops naming those two as
unavailable. There is no per-call ceiling on these calls; treg's own team
budgets apply. The receipt and the Observability page still show each call's
cost and endpoint, read from treg's answer inside the container.

The switch happens when waku connects its MCP servers
(`waku/tools/treg.py` `resolve()`), not in provisioning: the relay entry stays
in `mcp.json`, and clearing the key on the same dialog sends the next call
through the relay again. The gateway lets `treg` through `/api/connections`
with the `TREG_API_KEY` field and no other (`hosted/core/policy.py`
`CONNECTION_FIELDS`), so no save can point the server at another address or
name another credential. Nothing to do on the VM beyond the upgrade that
ships it.

Check it:

```bash
sudo docker compose --env-file /srv/waku/config/install.env \
  -f /srv/waku/src/hosted/deploy/compose.yaml --project-name waku \
  logs proxy | grep 'treg relay'      # "treg relay on, at most $0.5 a call"
```

Then ask a tenant's agent to find a TikTok profile through treg: the answer
comes back, and the charge appears in that person's waku.one usage.

**Turning it off** is `WAKU_TREG_TOKEN=` empty and `upgrade.sh`. The relay
answers 404 at once; the `treg` entries already in tenants' `mcp.json` stay,
because provisioning only ever adds, and show as a server that cannot connect.

**Tools the relay refuses are named to the model.** treg's `balance` and
`resources_list` read the platform team's own account, so the relay refuses
them. With `WAKU_TREG_RELAY=on` every tenant container starts with
`WAKU_UNAVAILABLE_TOOLS=treg_balance,treg_resources_list`, and the agent's
instructions say not to call them (spec 009 E). It is in the container's
environment, not in `SOUL.md`, so a tenant provisioned earlier gets it on its
next start.

**A tenant turn gets fifteen steps.** Every tenant container starts with
`WAKU_MAX_ITERATIONS=15` (`TENANT_MAX_ITERATIONS` in `spawner/template.py`),
the most model calls with tools one turn may make. At the limit the loop makes
one more call with tools off and answers from what the turn gathered. Like
the line above, a running tenant gets it on its next start; `upgrade.sh --now`
restarts every tenant onto it at once.

### A second fence: treg's per-customer daily budget

Every call is tagged `customer=<tenant id>`, so treg can cap each tenant per
day on its own side, whatever the proxy believes about their credits. Set a
default for every tenant once, from any machine signed in to treg, and
override one tenant when needed. `<org_id>` is the team's numeric id
(`treg org ls`), and the token must be an admin's:

```bash
printf 'treg token: '; read -rs treg_token; echo
printf 'X-Treg-Token: %s\n' "$treg_token" | curl -sS -X PUT -H @- \
  -H 'content-type: application/json' -d '{"daily_cap_micro": 1000000}' \
  "https://treg.to/orgs/<org_id>/budgets/customer"              # $1 a day, every tenant
printf 'X-Treg-Token: %s\n' "$treg_token" | curl -sS -X PUT -H @- \
  -H 'content-type: application/json' -d '{"status": "blocked"}' \
  "https://treg.to/orgs/<org_id>/budgets/customer/<tenant id>"  # cut one tenant off
unset treg_token
```

treg calls these caps advisory: concurrent calls can overshoot by about one
call each. The hard limits are the per-call ceiling and the team's prepaid
balance. `GET /orgs/<org_id>/usage/by-tag?key=customer&days=30` shows what each
tenant spent.

## What is not enabled yet

**No tenant firewall rules.** `deploy/firewall.sh` (task C3) is not in the
tree, so `install.sh` installs no firewall unit and says so. Tenant containers
cannot reach each other, because both bridges carry `enable_icc=false` -- but
they **can** reach the VM's private network and the cloud metadata service.
This is why the VM must carry no instance role.

## Operating it

Every script lives in `/srv/waku/src/hosted/deploy/` and runs as root.
`install.sh` puts nothing on `PATH`, so either use the full path or put the
directory on yours for the session:

```bash
export PATH=/srv/waku/src/hosted/deploy:$PATH   # or type the full path below
```

```bash
sudo tenant.sh status                 # who has a container running
sudo tenant.sh disable mei@example.com # status, sessions, token, container
sudo tenant.sh enable  mei@example.com
sudo tenant.sh delete  mei@example.com # archives the tree, removes the row
sudo tenant.sh inspect mei@example.com # a stock dashboard on their stopped data
sudo tenant.sh inspect-stop mei@example.com

sudo upgrade.sh                       # fetch, rebuild, restart the services
sudo upgrade.sh --now                 # and restart every running tenant too

sudo autodeploy.sh --enable           # upgrade on every green commit on main
sudo autodeploy.sh --status           # what is deployed, what failed, paused?
sudo autodeploy.sh --disable          # stop the timer

sudo backup.sh --all                  # what the nightly timer runs
sudo backup.sh --init-repository      # once, before the first backup
sudo backup.sh --snapshot-staged <id> # send a slot restic never received
sudo backup.sh --reset-staging <id>   # empty a wedged staging slot
sudo restore.sh --tenant mei@example.com # one tenant, from the latest snapshot
sudo restore.sh --all                 # the whole system, onto this VM

sudo migrate.sh --out                 # on the old VM
sudo migrate.sh --in                  # on the new one
```

`tenant.sh` takes a tenant id or an email address, and nothing else. It runs
`python -m hosted.gateway.admin` inside the gateway's container, because the
gateway holds the session cache, the container addresses and the tokens in
memory: a second process changing any of that behind its back would leave the
gateway serving from a cache it believes is still true.

`upgrade.sh` and the automatic-upgrade timer share one lock,
`/srv/waku/run/deploy/lock`. A manual `upgrade.sh` that starts while an
automatic upgrade runs refuses and says so; run it again when that finishes.

### Automatic upgrades

**Off until you turn it on.** With it on, a systemd timer checks `main` every
5 minutes and upgrades this VM to the newest commit on `main` once both
required checks, `skills-and-evals` and `hosted-docker`, have passed on that
exact commit and GitHub has verified its signature. It reads both from
GitHub's public API with no token, and it needs no inbound port and no cloud
permission.

Turn it on once, after an `upgrade.sh` by hand, so that the commit the VM is
running is the one the timer records as the last good one:

```bash
sudo /srv/waku/src/hosted/deploy/upgrade.sh
sudo /srv/waku/src/hosted/deploy/autodeploy.sh --enable
```

Each tick, in order:

1. If `/srv/waku/config/autodeploy.off` exists, it logs "paused" and stops.
2. It reads `main`'s commit with `git ls-remote` and stops when that commit is
   already deployed or already failed.
3. It refuses a commit that does not descend from the last deployed one, so a
   force-pushed `main` never deploys on its own.
4. It waits while either check is still running, and records the commit as
   failed when either check failed or was cancelled.
5. It writes a release record, `run/deploy/releases/<commit>.json`: the
   commit, the time, and each required check's result, link and test counts,
   all read from GitHub. A record it cannot write never stops the deploy.
6. It runs `upgrade.sh --ref <commit>`, then requires both the gateway on its
   internal address and `https://<your domain>/login` to answer 200.
7. If either fails, it runs `upgrade.sh --ref <last good commit>` and records
   the new commit as failed, so it is never retried. The next commit on
   `main` is.
8. **If the rollback fails too, it writes the kill switch itself** and stops.
   The site may be down at that point: run `upgrade.sh --ref <last good
   commit>` by hand and read the gateway's logs.

**An automatic upgrade can cut off a chat turn in progress.** `upgrade.sh`
recreates the gateway when its image changed, and every turn streams through
the gateway. The timer never passes `--now`, so a running tenant keeps the old
tenant image until their container next starts.

**A running tenant moves to the new image after 15 minutes idle.** Once a
minute the gateway stops every tenant container that has had nothing in flight
and no request other than a background poll for the idle window, and logs one
`idle stop tenant=<id>` line for each. The tenant's next message starts a new
container from the current tenant image. A gateway restart adopts the
containers already running and starts their idle clocks fresh. A tenant who
keeps chatting keeps the old image until they pause for the window, or until
an operator runs `upgrade.sh --now`, which restarts every running tenant at
once and can cut off a turn in progress.

**The first open after a stop starts the container, and waits for it.** The
request that finds a tenant stopped (after an idle stop or `upgrade.sh --now`)
waits up to 60 seconds for the spawner to provision and start the container
(`START_TIMEOUT_SECONDS` in `hosted/core/idle.py`), then up to 30 more for the
dashboard inside to accept a connection (`READY_TIMEOUT_SECONDS`): the spawner
answers once Docker has started the container, before the dashboard has bound
its port. A page navigation that outlasts either wait, the dashboard on the
tenant host or the chat in waku.one's frame, gets a 503 page saying the
assistant is starting, which reloads itself every 3 seconds and joins the
start already under way. A fetch or a stream still gets "Your assistant is
taking too long to start. Try again." A gateway log line `start of tenant=<id>
took longer than 60s` or `started but did not listen within 30s` is the sign
that a start ran long.

**What counts as idle.** In flight is any request the gateway is forwarding to
the container, from before it reaches the container until the last byte of a
streamed answer, so a container is never stopped mid-turn. Background requests
(the dashboard's timers, sent with `X-Waku-Background: 1`) never reset the idle
clock and never start a stopped container: they get `paused`, and the page
waits for the next user action. Two cases the gateway cannot see: a turn that
keeps running inside the container after the browser closed the tab, and work
the container starts on its own. Both stop with the container once the window
has passed since the last real request.

**Changing the window.** Set `WAKU_IDLE_MINUTES` in `config/gateway.env` to a
whole number of minutes, at least 1, and restart the gateway; it reads the
file only at startup and logs the window it is using. A `gateway.env` without
the line uses 15, so a VM installed before the idle loop needs no edit.

**A merge is now a production deploy.** The review on a pull request becomes
the last human look before this VM runs the code as root, about 10 minutes
after the merge.

The kill switch pauses every later tick without touching the timer; it does
not stop a tick that is already upgrading:

```bash
sudo touch /srv/waku/config/autodeploy.off   # pause
sudo rm /srv/waku/config/autodeploy.off      # resume
```

Read what it decided, one line per tick:

```bash
sudo journalctl -u waku-autodeploy --since today
sudo autodeploy.sh --status
```

Its state is in `/srv/waku/run/deploy/`: `deployed` holds the last commit
that passed every check, and `failed` lists the commits it will not retry,
with the time and the reason. Delete a line from `failed` to let that commit
be tried again.

**The Evals page shows the release each tenant runs.** `upgrade.sh` bakes the
record for the commit it checked out into the tenant image, at
`/etc/waku/release.json`, so every tenant's Evals page names the commit, when
it was deployed, and each required check with its result, a link to the run
and its test counts. The counts are real: each CI job prints them as a notice
(`scripts/ci_test_counts.py`) and `autodeploy.sh` reads that notice back from
GitHub. The AI judge needs an API key, so it is not in CI and the page says
so. A manual `upgrade.sh` to a commit the timer never recorded builds an image
without a record, and the page shows none. A running tenant sees the new
record when their container next starts, like any other image change.

### Looking at one person's data

**`tenant.sh inspect` never runs on the host.** A tenant can plant symlinks in
their own directories, so the dashboard runs in a throwaway container as their
UID, with only their two directories mounted, published on the VM's loopback.
Reach it over an SSH tunnel; the command prints the exact line.

The tenant stays in maintenance for as long as that dashboard exists. Their own
container will not start, and they see a maintenance message. **Run
`tenant.sh inspect-stop` when you are done**, or you have taken somebody's
assistant away and left no sign of why.

### Deleting a tenant

`tenant.sh delete` sets the status, ends the sessions, revokes the token, stops
the container, archives the tenant's two directories under
`/srv/waku/archive/<id>/`, and removes the row. It prints the two archive file
names.

**That archive is the only copy anything keeps.** Archives are in no restic
snapshot, and the nightly timer deletes them after 30 days. A deleted tenant is
also dropped from every later backup, because `backup.sh` walks the rows whose
status is `active` or `disabled`. So the archive is a grace period rather than
a backup: copy the two files somewhere else if the person may ask for their
data back.

**`delete` frees no disk today**, and an operator deleting a tenant to reclaim
space needs to know that before they do it. The live tree stays at
`/srv/waku/tenants/<id>` with its XFS project id, indefinitely; removing it is
task C of spec 001 and is not written. Until that lands, deleting a tenant
roughly doubles what they occupy rather than releasing it, because the archive
sits beside the tree rather than replacing it. Remove the tree by hand once you
are sure, checking first that nothing of theirs is running:

```bash
sudo tenant.sh status                                  # their id must not appear
sudo docker ps --filter label=waku.tenant=<id>         # and neither must this
sudo du -sh /srv/waku/tenants/<id> /srv/waku/archive/<id>
sudo rm -rf /srv/waku/tenants/<id>                     # the archive stays
```

**`tenant.sh status` alone is not enough for this, and the second command is
why.** `status` answers with the tenants whose own dashboard is running; an
`inspect` container and a backup or restore task container bind the same two
directories and `status` cannot name them. That is the whole reason `delete`
refuses for an inspected tenant, two paragraphs down, and a root `rm -rf` over
a live bind mount is the same hazard without the refusal.

`delete` refuses while an inspect container is still running for that tenant,
because the archive it would take is that tenant's only copy and a database
being written to by a live dashboard is a torn one. The refusal arrives after
the tenant has been disabled, so recover with `tenant.sh inspect-stop <id>` and
then either `tenant.sh delete <id>` again or `tenant.sh enable <id>`.

### Backups

A systemd timer runs `backup.sh --all` at 03:17 every night, with up to 15
minutes of random delay, and catches up when the VM was off at that hour.
Restic keeps 7 daily and 4 weekly snapshots per tenant and prunes the rest.

Every **tenant** snapshot carries the backup's own `manifest.json`, written
last. A restore refuses a tenant snapshot without one, because a backup that
did not finish cannot be told from an empty tenant by looking. The `control`
snapshot carries none and needs none: it holds two SQLite files, and a single
SQLite file carries its own completeness check, which both `backup.sh` and
`restore.sh` run as `PRAGMA integrity_check`.

`backup.sh` and `restore.sh` share one `flock` on the staging directory, so a
backup and a restore never run at once. List what is in the repository with
restic's own environment, which is exactly what `config/backup.env` holds:

```bash
sudo sh -c 'set -a; . /srv/waku/config/backup.env; set +a; restic snapshots'
```

### Restoring

`restore.sh --tenant <email or id>` restores one person. The gateway stops
their container first; nobody else is interrupted.

`restore.sh --all` restores the whole system in the spec's order: stop every
tenant container, stop the gateway and the proxy, replace `control.db` and
`ledger.db`, start both services, then restore every tenant one at a time. It
checks the control snapshot before it stops anything, so a repository that is
empty or unreachable costs a refusal and no downtime.

Two refusals you may meet, and what each one means:

- *"the gateway did not answer"*. `restore.sh --all` asks the running gateway
  to stop the fleet first. If the gateway is down or crash-looping, which is
  the disaster this command exists for, pass `--no-stop-fleet`. The fleet is
  still stopped once the gateway is back on the restored database, before any
  tenant tree is touched.
- *"already holds a finished backup that was never sent to restic"*. A backup
  finished and its upload failed, so the staging slot holds the only current
  copy of that tenant. Send it with `backup.sh --snapshot-staged <id>`, or
  throw it away with `backup.sh --reset-staging <id>`. Do not run
  `backup.sh --tenant <id>`: that re-copies the live tree over the good staged
  copy first.

### Moving to another VM

`migrate.sh` is two halves, one per machine, because a script on the old VM
able to SSH into the new one as root would be a credential on the old VM able
to take over the new one.

On the old VM, `migrate.sh --out` stops Caddy, stops every tenant container,
takes a final backup, stops the stack, and prints every flag the new VM's
`install.sh` needs along with the exact files to copy across. On the new VM,
after `install.sh` has run, `migrate.sh --in` restores everything from the
repository. Move the DNS records last: the certificate is issued by DNS-01, so
the new VM can hold it before any traffic moves.

Downtime is minutes, because all of the state is one directory tree and one
object store.

**`archive/` does not travel, and a migration ends the 30-day grace period
early.** The final backup `--out` takes covers every live tenant; the archives
of tenants deleted in the last month are in no restic snapshot, so they go away
with the old VM. `migrate.sh --out` now says so and prints the directory to
size; copy it across by hand if anything is in it.

**Rehearse it before you need it.** A migration is also the only end-to-end
proof that the backups are restorable, and the day you find out otherwise
should not be the day the VM is gone. With at least two tenants who have signed
in:

```bash
# On the OLD VM:
sudo /srv/waku/src/hosted/deploy/migrate.sh --out 2>&1 | tee /tmp/migrate-out.log
# Follow its printed steps on the NEW VM, then:
sudo /srv/waku/src/hosted/deploy/migrate.sh --in 2>&1 | tee /tmp/migrate-in.log
sudo /srv/waku/src/hosted/deploy/tenant.sh status
```

Then move the two DNS records and check four things as an existing tenant, in a
browser: they sign in, they land on the **same** tenant id, their provider is
still selected, and a memory they wrote before the final backup is still there.
Any of the four failing means the snapshot is not what you thought it was, and
you still have the old VM.

### If the VM is gone

`migrate.sh --out` is not available: it runs on the machine that died. You can
still get everything back from the restic repository, and the order is the whole
of it.

1. **Prove the repository is there before you build anything.** On any machine
   with `restic`, the repository address and the password:

   ```bash
   RESTIC_REPOSITORY=s3:s3.amazonaws.com/waku-backups \
     RESTIC_PASSWORD_FILE=./restic-password restic snapshots
   ```

   Do this first, not `backup.sh --init-repository`. On the wrong address
   `snapshots` says so and `--init-repository` would create an empty repository
   at your typo.
2. **Build the new VM through steps 1 to 6 above**, unchanged: the disk, the
   checkout, the DNS records already point wherever they pointed, and the three
   credential files written again from your password manager.
3. **Run `install.sh` with the flags you saved**, with `--data-device` changed to
   the new VM's disk. If you did not save them: `--free-model`,
   `--max-running`, `--tenant-disk`, `--supabase-audience` and `--acme-email`
   have to be remembered or guessed, and `--tenant-disk` defaulting to 1G will
   re-quota every tenant you restore.
4. **Append the object store credentials** to `/srv/waku/config/backup.env`
   again. Skip `backup.sh --init-repository`: the repository exists, and step 1
   proved it.
5. **`migrate.sh --in`.** It restores both platform databases and then every
   tenant, one at a time.
6. **Check one existing tenant** as the rehearsal above describes, then point
   the DNS records at the new VM.

What you cannot get back this way: the archives of tenants deleted in the last
30 days, which were only ever on the old VM.

## Where everything lives

```
/srv/waku/
  src/        this checkout. install.sh, the Compose file and the scripts
  config/     one env file per service, plus backup.env. Root-only, mode 0600
  control/    control.db: tenants, sessions, tokens. Owned by uid 10002
  ledger/     ledger.db: the spend ledger. Owned by uid 10003
  tenants/    one directory per tenant, home and env. Owned by uid 10001
  staging/    the handoff area backup.sh and restore.sh share
  archive/    deleted and pre-restore trees, kept 30 days
  run/        the four unix sockets the services talk over, and deploy/:
              the upgrade lock and the automatic upgrades' state
```

Read the logs with the same invocation every script here uses, which works from
any directory:

```bash
sudo docker compose --env-file /srv/waku/config/install.env \
  -f /srv/waku/src/hosted/deploy/compose.yaml --project-name waku \
  logs gateway
```

The same for `proxy`, `spawner` and `caddy`. `docker compose -p waku logs
gateway` also works while the project is running, because Compose v2 recovers
the project from the containers' own labels, but it has nothing to fall back on
once they are stopped.

The design behind all of this is [../docs/architecture.md](../docs/architecture.md)
for local waku, and the comments in [deploy/](deploy/) for the deployment.
