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
| `core/` | pure logic: tenant ids, route policy, quota, idle, provisioning, request validation, the connector list |
| `ports/` | the four replaceable seams, as Protocols and nothing else. The implementations live beside the service that owns each one |
| `gateway/` | the front door: login, sessions, routing by host, `control.db`, the idle loop, the scheduler that wakes a stopped tenant for an event or a timer, the public Slack app's install, events and send relay, the sign-in to a connector, and the home of a team space's agent |
| `proxy/` | the metering proxy in front of the model API, `ledger.db`, and the routes a container reaches the platform through: treg, outgoing Slack and a connector's tools |
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
5. The agent is not offered any tool whose name on that server starts with
   `ui.`. Those tools draw Waku Memory's own screens, and the hosted agent
   draws none (spec 039).

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
Agent (spec 004). The waku.one Waku Agent tab calls it. It is called server to
server:

```bash
curl -N https://agent.waku.one/v1/chat \
  -H "Authorization: Bearer <the person's Supabase access token>" \
  -H "Content-Type: application/json" \
  -d '{"message": "what did my competitors ship this week?"}'
```

The gateway verifies the token exactly as it does at sign-in, finds or creates
that person's tenant, mints their Waku Memory key if they have none, and relays
the container's own `/api/chat/stream`: Server-Sent Events, ending in a frame
with `"kind": "done"`. The first frame is `{"kind": "thinking"}`, sent before
the turn's first model call, and each tool call sends
`{"kind": "tool_start", "tool": "<name>", "call_id": "<id>"}` before it runs
and a `tool` frame with the same `call_id` after it (spec 039). A client
ignores any kind it does not know. The turn counts against the same hourly quota as a turn
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

## Slack: the public Waku app

One Slack app serves every person on the deployment (spec 026, group C). Ana
signs in at `https://agent.waku.one/setup`, presses "Add to Slack" and
approves in her workspace. From then on "@Waku remind the channel on Friday
at 09:00" in a channel reaches her agent, and the agent answers in the
thread, including while her container is stopped.

This is the hosted setup. A person who runs waku themselves creates their own
Slack app with Socket Mode instead; see
[integrations](../docs/integrations.md#slack-two-setups).

```
Slack --POST /v1/triggers/slack--> gateway --> tenant_events --> scheduler --> container
container --POST /slack/send--> proxy --gateway.sock--> gateway --chat.postMessage--> Slack
container --POST /slack/history--> proxy --gateway.sock--> gateway --conversations.history--> Slack
```

| Route | On | What it does |
|---|---|---|
| `GET /setup` | the apex | shows three steps and which are done: Sign in, Add to Slack, Mention @Waku. A connected workspace gets "Update permissions" and "Connect a different workspace", which both open Slack's approval page |
| `GET /hosted/slack` | a tenant's own host | tells the dashboard's Slack card what the gateway knows: whether this server has a Slack app, whether the person connected a workspace, its name, and the addresses of `/slack/install` and `/setup`. It reads one row, changes nothing, holds no token, and does not start the container |
| `GET /slack/install` | the apex | sends a signed-in person to Slack's approval page, with a `state` bound to their tenant that works once, for 10 minutes |
| `GET /slack/oauth/callback` | the apex | checks the `state` and the browser's own session, exchanges Slack's code for the workspace's bot token, and stores it |
| `POST /v1/triggers/slack` | the apex | takes every workspace's events. It checks Slack's signature over the raw body, refuses a timestamp more than 5 minutes from now, and stores a mention, or a direct message from the person who added the app, as one row in `tenant_events` under Slack's event id |
| `POST /slack/send` | the proxy, on the tenant network | takes a container's outgoing message with the container's platform token, and has the gateway post it. A tenant may send 30 a minute; one more answers 429, and the container tries it again after 10 seconds and then a minute |
| `POST /slack/history` | the proxy, on the tenant network | takes a container's request for the messages of one channel (the last ones, a window of days, or one person's), with the container's platform token, and has the gateway read them. The gateway reads a channel only when this tenant's agent was mentioned there. A tenant may read 20 times a minute |

A bad signature answers 403 and writes nothing. A workspace no tenant
connected answers 200 and writes nothing. A stored mention asks the scheduler
for a tick at once, and the tick is repeated every 2 seconds, at most 10
times, until the container has the event.

**The bot token never enters a container.** The gateway stores each
workspace's token in `control.db`, encrypted with AES-256-GCM under
`WAKU_SLACK_TOKEN_KEY`. The key is in `config/gateway.env` and nowhere else,
and `backup.sh` copies `control.db` and not `config/`, so a backup holds no
token that works. A container's message names a channel and a text. The
proxy finds the tenant from the platform token, and the gateway posts with
that tenant's token, which Slack accepts only in that tenant's workspace.

### Turning it on

Create the Slack app once at <https://api.slack.com/apps>, from scratch, in
the workspace that will own it. In the app's settings:

| Page | Field | Value |
|---|---|---|
| OAuth & Permissions | Redirect URLs | `https://agent.waku.one/slack/oauth/callback` |
| OAuth & Permissions | Bot Token Scopes | `app_mentions:read`, `chat:write`, `channels:history`, `groups:history`, `users:read`, `reactions:write`, `channels:read`, `groups:read`, `im:history` |
| Event Subscriptions | Request URL | `https://agent.waku.one/v1/triggers/slack` |
| Event Subscriptions | Subscribe to bot events | `app_mention`, `app_uninstalled`, `message.channels`, `message.groups`, `message.im` |
| App Home | Show Tabs | Messages Tab on, and "Allow users to send Slash commands and messages from the messages tab" ticked |
| Manage Distribution | Activate Public Distribution | on, so another workspace can add the app |

The last scope, the last event and the App Home row are what a direct
message needs. For an app created before 2026-11-20, add them in this order,
then have each workspace's person approve again:

1. **OAuth & Permissions**, "Scopes", "Bot Token Scopes": press "Add an OAuth
   Scope" and choose `im:history`.
2. **Event Subscriptions**, "Subscribe to bot events": press "Add Bot User
   Event", choose `message.im`, and press "Save Changes" at the bottom.
3. **App Home**, "Show Tabs": switch "Messages Tab" on and tick "Allow users to
   send Slash commands and messages from the messages tab".
4. If Slack's settings page asks you to reinstall the app, that reinstalls it
   in the app's own workspace only, and not through this gateway, so it
   records nobody. Do step 5 for that workspace too.
5. In each connected workspace, the person who added Waku opens
   `https://agent.waku.one/setup`, signed in, and presses "Update
   permissions", then "Allow" on Slack's page. That gives the workspace the
   new scope and records that person as the one whose direct messages are
   answered.

A workspace that skips step 5 keeps working in channels exactly as before:
Slack sends it no direct message at all.

Leave Socket Mode off. Slack checks the Request URL when you save it, so the
gateway has to be running with the four values below first.

Copy three values from the app's Basic Information page into
`/srv/waku/config/gateway.env`, as root:

```
WAKU_SLACK_CLIENT_ID=<Client ID>
WAKU_SLACK_CLIENT_SECRET=<Client Secret>
WAKU_SLACK_SIGNING_SECRET=<Signing Secret>
```

`install.sh` wrote the fourth line, `WAKU_SLACK_TOKEN_KEY`, with a key it
generated on the VM. A VM installed before spec 026 has no such line, because
`install.sh` and `upgrade.sh` keep an existing `gateway.env`. Add it once:

```bash
sudo bash -c 'printf "WAKU_SLACK_TOKEN_KEY=%s\n" \
  "$(od -An -N32 -tx1 /dev/urandom | tr -d " \n")" >> /srv/waku/config/gateway.env'
```

Never replace that key: the tokens already stored would stop opening, and
each person would have to add the app again. Then start the gateway on the
new file. A plain `restart` keeps the old environment, so recreate it:

```bash
sudo docker compose --env-file /srv/waku/config/install.env \
  -f /srv/waku/src/hosted/deploy/compose.yaml --project-name waku \
  up -d --force-recreate gateway
```

The gateway's log then says `Slack: on`. With a value missing it says `Slack:
off` and names the missing names, the gateway serves everything else, and
`/slack/install`, `/slack/oauth/callback` and `/v1/triggers/slack` answer 503
with the same names.

### Limits

- **One workspace has one tenant, and one tenant has one workspace.** A
  workspace stays with the tenant that connected it first, and everyone who
  mentions the agent there uses that tenant's agent. The tenant is a person,
  or the home of a team space's agent ([Team homes](#team-homes-a-team-spaces-agent-in-slack)).
  A second install by the same tenant replaces its workspace.
- **A mention in a thread arrives with the thread above it, and with
  nothing else.** The gateway asks Slack for that one thread, at most 30
  messages, and for the names in it (`slack_thread.py`). It has 2 seconds:
  past that the mention goes to the agent alone.
- **The agent reads the rest of the channel only when it asks** (spec 029).
  Its `slack_history` tool reads the channel the run is in, four ways: the
  last messages (30 by default, 100 at most); the messages of the last `days`
  days, 30 at most ("summarize this channel for the past week"); only the
  messages of one `person`, matched on the name Slack shows or its first
  word ("what did Emily say in the past month"); or one thread, from its
  first message. Two people who match a name are both named, and no message
  is returned. The tool names no channel, and the gateway refuses any channel
  the tenant's agent was never mentioned in.
- **A `slack_history` answer is bounded, and says when it was cut.** A window
  is read in at most 10 pages of 200 messages, and no page starts after 7
  seconds. An answer holds the channel's own messages, without thread
  replies, each with its count of replies, its author and its time in the
  tenant's time zone: at most 400 messages, 2,000 characters of each and
  40,000 bytes in all. Past any of those the newest part is kept, the answer
  carries `cut`, and the agent reads "Older messages in this window were
  left out". One request is one of the tenant's 20 reads a minute, however
  many pages it took. It needs `channels:history` or `groups:history`, and
  `users:read` for the names.
- **A reply in a thread the agent was mentioned in reaches it without a
  second mention.** Slack sends every message of a channel the bot is in
  (`message.channels`, `message.groups`). The gateway keeps one only when it
  is a person's reply in a thread that already holds a mention for this
  tenant, and drops the rest without storing them. The agent is told that
  people also talk to each other there, and that saying nothing is an answer.
- **A message the agent was given gets an eyes reaction at once**
  (`reactions:write`), so the person sees it was heard while a stopped
  container starts. A follow-up the agent then answers with silence keeps the
  reaction and gets no reply.
- **A direct message is answered for one person: the one who added the app**
  (spec 032, version A). The callback keeps the Slack user who pressed Allow
  (`authed_user.id` in Slack's answer), and a `message.im` event becomes a row
  only when Slack names that user as its sender. It needs no mention, the
  answer goes into the chat itself (into a thread only when the person wrote
  in one), and the message gets the eyes reaction. It is the same tenant, the
  same container and the same key as a mention in a channel. The row's
  `reply_to` carries `private: true`, written by the gateway and by nothing
  else, and the container reads it as its person's own chat: the sentence
  about a place several people read is not added, and a timer or a daily
  brief set there is delivered there. A direct-message id alone never means
  private.
- **Anybody else who writes to the bot directly gets one fixed line**, that it
  answers only the person who added it in a private chat for now and that
  they can mention it in a channel. At most once a day for each person, kept
  in the gateway's memory, so a restart can repeat it once. No row is stored,
  so no container starts and no model is called. An edit, a deletion, any
  other subtype and the bot's own messages are dropped.
- **A workspace added before 2026-11-20 has no recorded installer**, and
  neither has a team home. Nobody is treated as the owner there: everybody
  gets the fixed line. The person who added Waku presses "Update permissions"
  on `/setup` once, and the callback records them. Without that the workspace
  also lacks `im:history`, so Slack sends it no direct message in the first
  place.
- **`slack_history` does not read a direct message.** In a private chat the
  tool says that what was said there is already in the conversation.
- **Every channel the bot is invited to is heard.** The hosted app has no
  channel allowlist. A mention by a person from another workspace, in a
  shared channel, is dropped.
- **Replies need the free tier.** `POST /slack/send` is a route on the
  metering proxy, and a container knows the proxy's address only on a
  deployment installed with `--free-model`.
- **A restore onto a new VM needs `WAKU_SLACK_TOKEN_KEY` carried over by
  hand**, because no backup holds it. Without it each person is asked to
  reconnect ([Restoring](#restoring)).
- **Slack reviews a public app** before more than a handful of workspaces
  can add it.
- **None of this has run against Slack.** The evals use a fake Slack on
  loopback.

## Team homes: a team space's agent in Slack

A team space on waku.one has an agent of its own, with its own Waku Memory
key. A team home is where that agent runs on this deployment: one more
tenant, marked as a team, with its own container, disk and platform token
(spec 032, group A). Ana is an owner of the team space Acme. She presses "Add
Waku to Slack" on Acme's page on waku.one and approves in the workspace "Acme
HQ". From then on "@Waku remember that the launch moved to 3 December" in a
channel of Acme HQ reaches Acme's agent, on Acme's key, and not the agent of
any person.

```
waku.one API --POST /v1/team-homes--> gateway: tenant (kind team) + team_home (key sealed)
Ana's browser --GET /slack/install?home=<ticket>--> Slack's page --> /slack/oauth/callback --> Acme's page on waku.one
Slack --POST /v1/triggers/slack--> gateway --> tenant_events of Acme's home --> scheduler --> Acme's container
```

| Route | On | What it does |
|---|---|---|
| `POST /v1/team-homes` | the apex | makes the home of one space's agent from `space_id`, `space_name`, `agent_id`, `agent_name`, `memory_key`, `memory_api_url` and `return_url`. It answers `201` with `home_id` (the tenant's id) and `install_url`, and starts no container. A second call for the same space keeps the tenant and replaces the key |
| `GET /v1/team-homes/{space_id}` | the apex | answers `home_id`, `slack` (the workspace's `team_id` and `team_name`, or `null` until Slack's page was completed) and `refused` (always `null` for now). `404` when the space has no home |
| `DELETE /v1/team-homes/{space_id}` | the apex | answers `405` and changes nothing. Removing a home is group B of spec 032 |
| `GET /slack/install?home=<ticket>` | the apex | sends the owner's browser to Slack's approval page. The ticket names one home, works once and ends after 10 minutes. The page sets a cookie that lasts 10 minutes, and the callback installs only when that cookie and the `state` arrive together |

waku.one's API is the only caller of the three `/v1/team-homes` routes. Each
request carries `Authorization: Bearer <WAKU_AGENT_PLATFORM_SECRET>`. The
gateway compares the value in constant time and answers 401 to anything else,
before it reads the path or the body. It logs no request body.

**The team's key is sealed, and a person's key is not.** The gateway stores
the agent's Waku Memory key in `control.db`, in `team_home`, encrypted with
AES-256-GCM under `WAKU_SLACK_TOKEN_KEY` and bound to the space's id. A
backup of `control.db` holds no team key that works. The key leaves the
gateway in two places. A start request carries it to the spawner, which sets
`WAKU_MEMORY_API_KEY` and `WAKU_MEMORY_SPACE=<space id>` in the home's
container. The proxy receives it when it resolves the home's platform token,
and charges the home's model calls with it. A home whose key does not open
is not started.

**What the agent saves belongs to the team.** With `WAKU_MEMORY_SPACE` set,
stock waku sends `space` with every `memory.remember` it makes itself: a
fact `save_note` saves, a fact consolidation keeps and a research report.
Every Slack event of a home starts with one line: "You are Waku, the agent of
the team space Acme. What you save here is read by every member of Acme."

### Turning it on

Make one secret and enter it in two places by hand: in
`/srv/waku/config/gateway.env` on the VM, and on waku.one as its own
`WAKU_AGENT_PLATFORM_SECRET`. waku.one also needs `WAKU_AGENT_PLATFORM_URL`,
which is `https://agent.waku.one`.

```bash
openssl rand -hex 32          # 64 characters; the gateway refuses fewer than 32
```

```
WAKU_AGENT_PLATFORM_SECRET=<the value>
```

Never paste the value into a chat, an issue or a commit: whoever holds it can
make a team home here. The routes also need `WAKU_SLACK_TOKEN_KEY`, and the
install page needs the three other Slack values
([Slack](#slack-the-public-waku-app)). Then recreate the gateway, as for
Slack. Its log says `team homes: on`. With the secret missing it says `team
homes: off`, and every `/v1/team-homes` route answers 503 and names the
variable.

### Limits

- **A workspace that a person's own install holds is left as it is.** An
  owner who approves that workspace for a team space changes nothing, and
  `GET /v1/team-homes/{space_id}` keeps answering `slack: null`. The same is
  true of a workspace another space's home holds. Moving a workspace, and
  saying which space holds it, are groups B and E of spec 032.
- **A replaced key reaches a running container at its next start.** A second
  `POST` stores the new key and does not restart the home (group B).
- **`return_url` is checked for its shape only**: `https`, or `http` on
  localhost. `memory_api_url` is not checked against this deployment's own
  Waku Memory address. Both are group B. Until then the platform secret is
  what keeps a browser from being sent somewhere else.
- **A home has no page and no sign-in.** Nobody opens its dashboard, and
  `tenant.sh` names it by its tenant id: its email is empty.
- **A home answers no direct message.** Whoever writes to the bot directly in
  a home's workspace gets the fixed line of a direct message
  ([Slack](#slack-the-public-waku-app), "Limits"). A person's own agent in a direct message, from
  a link to their Waku account, is group C. A reply carries no link to the
  memory it saved (group F).
- **The model's own `memory.remember` tool call carries no `space`** unless
  the model writes one. Only the three sends named above add it.
- **A restore onto a new VM needs `WAKU_SLACK_TOKEN_KEY` carried over by
  hand**, as for Slack. Without it no team key opens and no home starts,
  until waku.one sends each key again with a new `POST`.
- **None of this has run against waku.one, Slack or Waku Memory.** The evals
  use a fake Slack and a fake spawner on loopback.

## Connectors: Notion, Linear, GitHub and Gmail

A person on hosted waku signs in to Notion, Linear or GitHub from
`https://agent.waku.one/setup` (spec 029). Ana presses "Connect Notion",
approves on Notion's own page, and comes back to `/setup` with "Notion is
connected." The gateway then holds her Notion tokens.

A person who runs waku themselves uses `waku mcp add` instead, and the
sign-in comes back to a port on their own computer
([integrations](../docs/integrations.md)).

| Route | On | What it does |
|---|---|---|
| `GET /connect/start?server=<name>` | the apex | sends a signed-in person to the vendor's sign-in page, with a `state` bound to their tenant and the vendor that works once, for 10 minutes, and a PKCE challenge |
| `GET /connect/callback` | the apex | checks the `state` and the browser's own session, exchanges the vendor's code for the person's tokens, and stores them |
| `POST /connect/disconnect` | the apex | takes `{"server": "<name>"}` from a signed-in person and deletes their tokens for that vendor |
| `POST /connect/<name>/mcp` | the proxy, on the tenant network | takes one MCP message from a container with the container's platform token, and has the gateway make the call with that tenant's sign-in. A tenant may send 60 a minute to one connector; one more answers 429 |
| `POST /connect/link` | the proxy, on the tenant network | answers the `/connect/start` address for one connector, so "connect Notion" in a chat can give the person the link. Asked for a name that is not a connector here, it answers the titles of the ones that are. A container's agent asks once when it builds its tools, so `connect_tool` and `connector_status` name these connectors to the model and no others |

```
container --POST /connect/notion/mcp--> proxy --gateway.sock--> gateway --> mcp.notion.com
```

`<name>` is `notion`, `linear` or `github` (`hosted/core/connectors.py`). A
person cannot add another address: the gateway calls a connector from the
platform's own network. GitHub is on a server's list only when that server
holds a GitHub App's client id and secret ("GitHub" below).

**The gateway registers itself with Notion and Linear.** At the first sign-in to a
vendor it reads the vendor's sign-in addresses, registers as a client with the
one redirect address `https://agent.waku.one/connect/callback` (dynamic
client registration), and keeps that registration in `control.db` for every
tenant. Nobody creates an app at Notion or Linear, and no client id is copied
into a config file. Each address the vendor answers with has to be on the
origin of the vendor's own MCP address, or the sign-in does not start.

**A vendor's tokens never enter a container.** The gateway stores the access
token and the refresh token in `control.db`, encrypted with AES-256-GCM under
`WAKU_SLACK_TOKEN_KEY`, the key that seals a workspace's Slack token. Each
value is bound to its tenant and its vendor, so one row's value does not open
in another row. The gateway renews an access token in its last minute, and a
vendor that refuses the renewal puts "Reconnect Notion" on the person's
`/setup`.

**The container sees an MCP server at the proxy.** When a person signs in to
Notion, the gateway has the spawner provision their tenant again, and
provisioning writes a `notion` server into the tenant's `mcp.json`:
`{"name": "notion", "url": "http://10.88.0.1:8788/connect/notion/mcp",
"auth_env": "WAKU_PLATFORM_TOKEN"}`. The container is not restarted: its agent
reads `mcp.json` before each run and builds itself again when the list
changed. Disconnecting removes the entry the same way. A server the tenant
added themselves under the same name is left alone.

**The gateway reads every message, and a tool that writes is not run.** A
container may send `initialize`, `tools/list`, `tools/call` and `ping`, and
anything else is refused. A `tools/call` whose tool matches the vendor's
write patterns (`hosted/core/connectors.py`, the same patterns as
`waku/connectors.toml`) is answered by the gateway with "Not run:
'notion-create-pages' changes something in Notion, and a change needs your yes
first. Saying yes is not built on hosted Waku yet." Notion is never called for
it. Spec 029's group C adds the yes.

The gateway keeps the vendor's `Mcp-Session-Id` itself, per tenant and
vendor, and opens a new session when the vendor has forgotten one or the
gateway restarted. It reads the vendor's answer to its end, as JSON or as an
event stream, compressed or not, and returns at most 1 MiB of it as plain
JSON. A message from a container is at most 48 KB.

### GitHub

GitHub's MCP server does not let a client register itself: "Dynamic Client
Registration is NOT supported by Remote GitHub MCP Server at this time"
([host integration](https://github.com/github/github-mcp-server/blob/main/docs/host-integration.md),
read 2026-11-19). The operator registers one GitHub App, once, and the
gateway signs every person in through it. It is a GitHub App and not an OAuth
App because an OAuth App's only scope for a private repository, `repo`, also
writes.

Register the app at github.com: your profile picture, **Settings**,
**Developer settings**, **GitHub Apps**, **New GitHub App**.

| Field | Value |
|---|---|
| GitHub App name | a name nobody else has, for example `Waku Agent` |
| Homepage URL | `https://agent.waku.one` |
| Callback URL | `https://agent.waku.one/connect/callback` |
| Expire user authorization tokens | ticked |
| Request user authorization (OAuth) during installation | not ticked. Ticked, GitHub sends a browser to the callback without the gateway's `state`, and the page answers "That link expired" |
| Enable Device Flow | not ticked |
| Webhook, Active | not ticked |
| Repository permissions | **Contents**, **Issues**, **Pull requests** and **Metadata**: "Read-only". Every other permission: "No access" |
| Where can this GitHub App be installed? | "Any account", so a person can install it on their own repositories |

Press **Create GitHub App**. On the app's page, copy the **Client ID** and
press **Generate a new client secret**. Put both into
`/srv/waku/config/gateway.env`, as root, and restart the gateway:

```
WAKU_GITHUB_CLIENT_ID=<Client ID>
WAKU_GITHUB_CLIENT_SECRET=<the client secret>
```

The gateway's log then says `connectors: on (notion, linear, github)`.
Without both values it says why GitHub is not offered, `/setup` has no GitHub
card, and an agent asked to connect GitHub answers that it cannot be
connected here yet.

A person presses "Connect GitHub" on `/setup` and approves on github.com. The
agent then reads the repositories the app is installed on, so the person also
installs the app from its public page, `https://github.com/apps/<the app's
name>`, on the account or the repositories the agent may read.

**Reading is enforced three times.** The app's permissions are read-only, so
GitHub refuses a write with the person's token. The gateway calls
`https://api.githubcopilot.com/mcp/readonly`, where GitHub's server offers no
tool that writes. And the gateway refuses a `tools/call` whose tool matches
GitHub's write patterns before GitHub is called, as it does for Notion.

GitHub's token lasts eight hours and its refresh token six months. The
gateway renews the token in its last minute and keeps the new refresh token,
because GitHub ends the old one when it is used.

### Gmail

A person connects one Gmail mailbox, and their agent can search it, read a
thread and write a draft reply in it (spec 031, part A). **It cannot send.**
Google has no MCP server a client can sign in to, so Gmail is not relayed as
one: the gateway calls Gmail's own API.

```
container --POST /mail/read--> proxy --gateway.sock--> gateway --> gmail.googleapis.com
```

| Tool in the container | Route on the proxy | What the gateway asks Gmail |
|---|---|---|
| `mail_search` | `POST /mail/search` `{"query", "limit"}` | `messages.list` with Gmail's own search, then each message's headers. 10 by default, 20 at most |
| `mail_read` | `POST /mail/read` `{"thread_id"}` | `threads.get`. The last 10 mails of the thread, 4,000 characters of each, without the lines a reply quotes |
| `mail_draft` | `POST /mail/draft` `{"thread_id", "body"}` | `threads.get`, then `drafts.create` in that thread |

**Nothing sends.** The proxy's route is `/mail/search`, `/mail/read` or
`/mail/draft`, and `/mail/send` is a 404. The gateway's socket takes those
three actions and no other. `hosted/gateway/mail.py` holds five Gmail
addresses, four that read and `drafts.create`, and an eval reads the file to
keep a sending address out of it. Google's `gmail.compose` scope would let a
client send a draft; what stops it is that no code here asks. The person
opens Drafts in Gmail and presses Send.

**The gateway addresses the draft.** The agent gives a thread and a text. The
reply goes to the last mail in the thread that the person did not write, at
its `Reply-To` or else its `From`, so a mail that says "send this to
somebody" cannot put an address on a draft. There is no draft outside a
thread yet.

**A mail is other people's text.** The tool puts everything read from Gmail
between two lines that carry a mark made for that one call, under a sentence
that says it is information and never an instruction. A mail cannot know the
mark, so it cannot close the frame itself.

**One mailbox per person, for now.** The spec wants several. `control.db`
keeps one sign-in per tenant and connector, so a second sign-in replaces the
first.

The operator sets it up once, in the Google Cloud console
(`console.cloud.google.com`), project `waku-agent`:

1. **APIs & Services**, **Enabled APIs & services**: "Gmail API" is listed.
   If it is not, **Enable APIs and services**, search "Gmail API", **Enable**.
2. **Google Auth Platform**, **Audience** (in the older console, **OAuth
   consent screen**): publishing status is "Testing". Under **Test users**,
   **Add users**, and enter every Google address that will connect a mailbox.
   Only these addresses can sign in, up to 100.
3. **Google Auth Platform**, **Data Access**, **Add or remove scopes**: tick
   `https://www.googleapis.com/auth/gmail.readonly` and
   `https://www.googleapis.com/auth/gmail.compose`, **Update**, **Save**.
   Not `gmail.send` and not `gmail.modify`.
4. **Google Auth Platform**, **Clients** (in the older console, **APIs &
   Services**, **Credentials**), the client "Waku hosted agent", type "Web
   application". Under **Authorized redirect URIs** it has exactly
   `https://agent.waku.one/connect/callback`.
5. On that client's page, copy the **Client ID**. Under **Client secrets**,
   **Add secret**, and copy it then: Google shows a secret once.

Put both into `/srv/waku/config/gateway.env`, as root, and recreate the
gateway with the command under "Slack: the public Waku app", "Turning it on":

```
WAKU_GOOGLE_CLIENT_ID=<Client ID, ending in .apps.googleusercontent.com>
WAKU_GOOGLE_CLIENT_SECRET=<the client secret>
```

The gateway's log then says `connectors: on (notion, linear, github, gmail)`.
Without both values it says why Gmail is not offered, `/setup` has no Gmail
card, an agent has no mail tools, and one asked to connect Gmail answers that
it cannot be connected here yet. A container builds its tools when it
starts, so an agent that was running before the values were added gets the
three tools at its next start.

A person presses "Connect Gmail" on `/setup`, or says "connect Gmail" to
their agent and opens the link. Google shows "Google hasn't verified this
app" while the app is in testing: **Continue**, then leave both permissions
ticked, then **Continue**.

**In testing mode Google ends a sign-in after 7 days.** The gateway's renewal
is then refused, `/setup` says "Reconnect Gmail", and the mail tools answer
with the link to sign in again. This ends when the app passes Google's
review, which is not started (spec 031, open question 1).

### Turning it on

Connectors are on when `config/gateway.env` has a usable
`WAKU_SLACK_TOKEN_KEY`, which `install.sh` writes ("Slack: the public Waku
app" above says how to add one to an older VM). They need none of the other
three Slack values. The gateway's log says `connectors: on (notion, linear)`,
or `connectors: off` with the reason, and the three routes then answer 503.

### Limits

- **Linear is asked for `read` alone**, and Notion for `default`, the one
  scope its server names.
- **A connector's tools need the free tier**, as a Slack reply does:
  `POST /connect/<name>/mcp` is a route on the metering proxy, and a container
  knows the proxy's address only on a deployment installed with
  `--free-model`. Without it a person can sign in, and no server is written
  into their `mcp.json`.
- **A connector's write tools never run**, whatever the person says in chat.
  A write tool whose name matches none of the vendor's write patterns would
  run: the patterns list verbs, and a verb can be missing.
- **Calls are not charged credits.** There is no limit by plan: every person
  may connect all three.
- **No button disconnects yet.** `POST /connect/disconnect` is the route; the
  Connections page (spec 030) is where its button goes.
- **Disconnecting deletes the gateway's copy of the tokens and does not
  revoke them at the vendor.** The person removes Waku in the vendor's own
  settings to end the grant there.
- **A restore onto a new VM needs `WAKU_SLACK_TOKEN_KEY` carried over by
  hand**, as for Slack. Without it each person signs in to their connectors
  again, and the gateway registers with each vendor again.
- **Removing the two GitHub values takes GitHub off the list** and leaves
  each person's sealed tokens in `control.db`. Their `github` tools answer an
  error until the values are back.
- **Removing the two Google values takes Gmail off the list** the same way.
- **Gmail has run against a fake Google only.** Not verified against Google:
  that its sign-in accepts the PKCE challenge beside the client secret, the
  `iss` it sends back, and how Gmail fills a draft's `From`. A mail's
  attachments are not read, a mailbox's new mail does not wake the agent
  (spec 031, decision 3), and a mail call is not charged credits.
- **None of this has run against Notion, Linear or GitHub.** The evals use a
  fake vendor on loopback. Whether each vendor's registration accepts
  `https://agent.waku.one/connect/callback` as a redirect address is not
  verified.

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
  When `count_tokens` fails, the reservation uses an estimate of one token per
  two bytes of the request, which is always above the real count, and the call
  still runs.
  A call with a cache breakpoint reserves the prompt-cache tokens the person's
  last call on that model read or wrote, within the last five minutes, at the
  cache read price ($0.10 a million on Sonnet 5.5); with no such call, it
  reserves its input at the cache write price (spec 039).
  A person at their dollar never reaches Anthropic, and is refused with the
  same `credits_used_up` answer as below, in the Free sentence.
- **Bedrock behind Anthropic** (spec 043). With `WAKU_BEDROCK_KEY` set in
  `config/proxy.env`, a turn Anthropic refuses (a usage limit, a bad key, an
  outage, no connection) goes to `WAKU_BEDROCK_MODEL` (GLM-5 by default) on
  Bedrock's chat-completions address, and Anthropic is skipped for ten
  minutes. The proxy translates the Anthropic request and stream both ways,
  so containers do not change, and charges the turn at GLM-5's price. The
  switch happens only before anything has streamed. `WAKU_UPSTREAM_PRIMARY=bedrock`
  sends every turn to Bedrock. Empty `WAKU_BEDROCK_KEY` turns Bedrock off.
- **The limits.** 4 calls at once and 60 a minute per person, 16 at once
  across the VM, `max_tokens` at most 8192.
- **What it refuses.** Images, documents, server tools, a `cache_control`
  other than `{"type": "ephemeral"}` (the one-hour form included), and any
  body field outside the allowlist. Thinking blocks Sonnet returns are accepted
  back, because Waku sends them on the next call. The five-minute
  `cache_control` is accepted (spec 039), so the calls of one turn read the
  tools and system prompt from Anthropic's prompt cache.

- **One wallet.** Each settled call is also charged to the person's waku.one
  credits, at 37,500 credits a dollar (1.5× model cost), with their own Waku Memory key (spec
  004 D; `POST /agent-usage`). Waku Memory decides who may still call: its
  `GET /agent-usage/balance` answers `allowed` and `notice` (spec 042), read
  once a minute per person. A person with `allowed: false`, on any plan, is
  refused before their call reaches Anthropic with a 403 whose body is
  `{"type": "error", "error": {"type": "permission_error", "code":
  "credits_used_up", "message": "<notice>"}}`. The notice names the price and
  links to `https://www.waku.one/settings/plan`, and every channel shows it to
  the person in place of an error. An older Waku Memory that sends no
  `allowed` keeps the earlier rule: a Free person at zero is refused, and a Pro
  person may go below zero. When Waku Memory cannot be reached, the $1 cap is
  the only limit.
- **What a turn cost.** Each model call from a container carries
  `X-Waku-Turn: <turn_id>`, which the proxy reads and never forwards to
  Anthropic. For one hour, in memory, the proxy keeps each turn's settled
  dollars, its call count, and the credits Waku Memory answered as `charged`
  for those calls and for the treg calls the relay charged during the turn.
  `GET /v1/turns/<turn_id>/charges`, with the container's own platform token,
  answers `{"model_usd": 0.0712, "calls": 5, "credits": 2400,
  "credits_state": "low"}`; another
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
  so is a person Waku Memory says is out of credits, with the model call's
  own `credits_used_up` answer. When Waku Memory cannot be reached, the
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
credits." and has a Manage button. They paste their org-scoped treg key,
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

**A tenant turn re-sends six earlier turns.** Every tenant container starts
with `WAKU_HISTORY_TURNS=6` and `WAKU_HISTORY_ANSWER_CHARS=1500`
(`TENANT_HISTORY_TURNS` and `TENANT_HISTORY_ANSWER_CHARS` in
`spawner/template.py`). Each model call of a turn sends the six turns before
it, with each earlier answer cut to 1,500 characters; the chat log keeps every
answer whole. A laptop keeps twelve turns, uncut (spec 039).

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
the gateway. The timer never passes `--now`, so no tenant container is
restarted by the upgrade itself.

**A running tenant moves to the new image between two turns.** A container
keeps the image it was created from, and an upgrade rebuilds the tenant image
under it. Once a minute the gateway asks the spawner which running containers
were created from an older build than the one the image tag names now. It
stops each of those that has nothing in flight, has had no request other than
a background poll for 60 seconds (`STALE_QUIET_SECONDS` in
`hosted/core/idle.py`), and reports no run in progress, no unfinished event
and no unsent message. It logs one `stale stop tenant=<id>` line for each. The
container's next timer is stored first, as at an idle stop, and the tenant's
next message or that timer starts a new container from the current image. A
person who sends a message every few minutes therefore runs the new code about
a minute or two after an upgrade, at the cost of one container start. A
container that reports work, or that does not answer the question, is not
stopped this way: it waits for the idle stop below. `upgrade.sh --now` still
restarts every running tenant at once and can cut off a turn in progress.

**An idle container stops after 15 minutes.** Once a minute the gateway also
stops every tenant container that has had nothing in flight and no request
other than a background poll for the idle window, and logs one `idle stop
tenant=<id>` line for each. The tenant's next message starts a new container
from the current tenant image. A gateway restart adopts the containers already
running and starts their idle clocks fresh.

**The first open after a stop starts the container, and waits for it.** The
request that finds a tenant stopped (after an idle stop, a stale stop or `upgrade.sh --now`)
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
waits for the next user action.

**A container that is still working is not idle.** Before the gateway stops a
container that has passed the window, it asks the container what work it has
left (`GET /api/alwayson/next`). The container is kept while it reports a run
in progress, an event no run has finished with, or an outgoing message not yet
sent. A run that waits for the owner's yes does not keep it: the yes arrives
as an event. A container that still reports work 2 hours past the window
(`WORK_LIMIT_SECONDS` in `hosted/core/idle.py`) is stopped anyway, and so is
one that does not answer, which includes every container on an image older
than this rule. One case the gateway still cannot see: a chat turn that keeps
running inside the container after the browser closed the tab. It stops with
the container once the window has passed since the last real request.

**What still works while a container is stopped.** The gateway keeps two
tables in `control.db` for a tenant whose container is not running, and a
scheduler reads both every 30 seconds (`hosted/gateway/scheduler.py`).

| Table | Holds | What the scheduler does |
|---|---|---|
| `tenant_events` | one row per event for a tenant, unique on tenant, source and the sender's own id | starts the tenant's container, posts the rows to it (`POST /api/events`), and marks them `handed` when the container answers 200 |
| `tenant_wakeups` | the time of the tenant's earliest timer, as the container reported it before it was stopped | starts the container at that time; the container's own timer loop then fires the timer |

A wake goes through the same running cap as a request, so at the cap it stops
the container idle longest, and it waits when none can be stopped. An event
that could not be handed over stays `pending` and is offered again 30 seconds
later. The container stores each event once, so an event offered twice is one
run. A browser cannot call either route: the gateway refuses `POST
/api/events` and `/api/alwayson/next` from a signed-in person, and the
container answers them only when it was started with `WAKU_HOSTED=1`, which
the spawner sets on a tenant container and not on an inspect container.

Three limits as of this change. Only a Slack mention writes a row into
`tenant_events` ([Slack](#slack-the-public-waku-app)): chat and the embed
arrive in group B of spec 026. A hosted container sends to Slack and nowhere
else, so a run that a timer starts in a conversation that is not a Slack
thread answers in that conversation's history and sends nothing. And a wakeup
is stored only at an idle stop or a stale stop: a container stopped by an
eviction at the cap, by `upgrade.sh --now` or by a VM reboot keeps the wakeup
stored at its last such stop, so a timer set after that fires when the tenant
next opens the dashboard.

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

A tenant's `state.db` is copied with SQLite's own `.backup`, inside the
tenant's task container, and `state.db`, `state.db-wal` and `state.db-shm`
are left out of the file copy that follows. `state.db` runs in WAL mode, so a
row that was just written sits in `state.db-wal` until SQLite moves it into
`state.db`, and a file copy of `state.db` alone would miss it. The staged file
is a whole database, so `restore.sh` copies it back as a file.

**One thing is left out on purpose: `WAKU_SLACK_TOKEN_KEY`.** `control.db` is
backed up and holds each workspace's Slack token encrypted. The key that opens
them is a line in `/srv/waku/config/gateway.env`, and `config/` is never backed
up, so a copy of the repository holds no Slack token that works. Keep a copy
of that one line somewhere other than the VM and the repository. `backup.sh
--all` prints a line saying so every night.

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

**After a restore onto another VM, Slack needs its key.** The restored
`control.db` holds Slack tokens that open only with the old VM's
`WAKU_SLACK_TOKEN_KEY`. Copy that line into the new VM's
`/srv/waku/config/gateway.env` and recreate the gateway, and every workspace
stays connected. Without it the gateway still starts, logs how many tokens it
cannot open, and shows each affected person "Reconnect Slack" on `/setup`;
adding the app again stores a new token. `restore.sh --all` prints a line
saying so.

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
  config/     one env file per service, plus backup.env. Root-only, mode 0600.
              gateway.env holds the Slack app's secrets, the secret
              waku.one's calls carry, and the key that encrypts Slack tokens
              and each team's Waku Memory key in control.db
  control/    control.db: tenants, sessions, tokens, waiting events, each
              tenant's Slack workspace and each team space's home. Owned by
              uid 10002
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
