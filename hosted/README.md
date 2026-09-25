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
| `ports/` | the four replaceable seams, as Protocols and nothing else. The implementations live beside the service that owns each one |
| `gateway/` | the front door: login, sessions, routing by host, `control.db` |
| `proxy/` | the metering proxy in front of the model API, and `ledger.db` |
| `spawner/` | the one process that talks to Docker |
| `image/` | the tenant and services Dockerfiles, their allowlist ignore files, and the seccomp profile every container runs under |
| `templates/` | the hosted `SOUL.md` and the gateway's own pages |
| `deploy/` | `install.sh`, Compose, Caddy, the operator scripts, and the example env files they write |

## Before you install

You need six things. Collect all six before you touch the VM: `install.sh`
refuses without any of them, and two of the refusals land after the VM has been
changed.

| You need | Why | How it is given |
|---|---|---|
| An Ubuntu 24.04 VM | The Docker packages, the systemd units and the resolver path are that release's | `install.sh` reads `/etc/os-release` and refuses anything else |
| A second disk for `/srv/waku`, XFS with project quotas | Each tenant gets a disk limit, and XFS project quotas are what enforce it | `--data-device /dev/sdb1` |
| A domain you control through a DNS API | The certificate is issued by DNS-01, so Caddy answers the challenge by writing a TXT record | the apex is the first argument; the API credentials go in `--dns-env-file` |
| A Supabase project with asymmetric signing keys and signup turned off | The gateway verifies access tokens with a public key, and this deployment is invite-only | `--supabase-url`, `--supabase-publishable-key`, `--supabase-audience` |
| A model API key for the platform | The metering proxy calls Anthropic with it. No tenant container ever holds it | `--platform-key-file` |
| A restic repository and its password | Every tenant's data is backed up into it nightly | `--restic-repository`, `--restic-password-file` |

Memory decides how many tenants can run at once. `install.sh` sets the cap to
(memory minus 2 GB) divided by 150 MB, so a 16 GB VM runs 95 containers at
once. Pass `--max-running N` to choose your own number. Idle containers stop on
their own, so the cap limits concurrency and not how many people can sign up.

### The data disk

`/srv/waku` must be XFS mounted with `prjquota`. Without it every tenant shares
one unbounded disk, and `install.sh` refuses. On a second disk at `/dev/sdb1`:

```bash
mkfs.xfs -q /dev/sdb1
mkdir -p /srv/waku
echo '/dev/sdb1 /srv/waku xfs defaults,prjquota 0 2' >>/etc/fstab
mount /srv/waku
xfs_quota -x -c 'state -p' /srv/waku    # expect: Enforcement: ON
```

Then clone this repository onto that disk, so the checkout and the data move
together:

```bash
git clone https://github.com/ShenSeanChen/waku-agent.git /srv/waku/src
```

### DNS and TLS

Create two records before you install, both pointing at the VM's public
address:

```
agent.example.com       A    203.0.113.10
*.agent.example.com     A    203.0.113.10
```

The wildcard is what gives every tenant their own origin at
`<id>.agent.example.com`. One certificate covers both names.

**Caddy gets the certificate through DNS-01, not HTTP-01.** A wildcard
certificate cannot be issued any other way, so Caddy has to write a TXT record
in your zone, and it needs your DNS provider's API credentials to do it.
`install.sh` builds Caddy from source with the `caddy-dns` module you name in
`--dns-provider`: pass `route53`, `cloudflare`, `digitalocean` or whichever
module matches your provider, exactly as it appears under
`github.com/caddy-dns/`. A module whose Caddy directive takes an inline
argument is written whole, for example
`--dns-provider 'cloudflare {env.CLOUDFLARE_API_TOKEN}'`. Pin it with
`--dns-module-version @v1.5.0` if you want a rebuild next month to produce the
same Caddy.

**Stop and disable any Caddy you are already running, before you run
`install.sh`.** This deployment runs Caddy as a container built with your
`caddy-dns` module, and it binds ports 80 and 443 on the host. A Caddy you
installed by hand holds those ports, so the two cannot coexist. `install.sh`
does not stop it for you: stopping a service you built yourself is not an
installer's business, and it would take your holding page down without asking.
The moment you run the two commands below is the moment that page stops
serving.

```bash
systemctl stop caddy
systemctl disable caddy
```

`install.sh` refuses before it changes anything if either port is still held by
something that is not this deployment's own Caddy container, and its refusal
names the port, the process and the container. That refusal is the first check
it runs, ahead of the apt install and both image builds, so a contested port
costs you a message and nothing else.

### The Supabase project

`install.sh` reads two public endpoints of your project and refuses on either:

- The project must publish **asymmetric** signing keys (ES256 or RS256). With
  HS256 the gateway would have to hold the secret that mints tokens, and it
  only ever needs the public key that verifies them.
- The project's public auth settings must show **signup closed**. Hosted waku
  is invite-only; a free tier with open signup is an abuse target.

### The three credential files

`install.sh` takes three secrets as **files**, never as values on the command
line. A value typed as an argument lands in root's shell history and in `ps`
output for the whole install, and the installer can clean up neither.

| File | Flag | What happens to it |
|---|---|---|
| The platform's model key | `--platform-key-file` | Copied into `/srv/waku/config/proxy.env` at mode 0600. Delete the source file afterwards |
| The DNS provider's credentials, as `NAME=VALUE` lines | `--dns-env-file` | Copied into `/srv/waku/config/caddy.env` at mode 0600. Delete the source file afterwards |
| The restic repository's password | `--restic-password-file` | **Not copied.** `config/backup.env` names the path, and restic opens the file every night |

Write the restic password before you install, because `install.sh` reads it as
part of its argument checks and refuses an unreadable or empty file. It can
live anywhere root can read; keeping it beside the rest of the config is one
less path to remember:

```bash
mkdir -p -m 0700 /srv/waku/config
( umask 077; head -c 32 /dev/urandom | base64 >/srv/waku/config/restic-password )
```

**Keep a copy of the restic password somewhere that is not this VM.** It is the
only one of the three that is not recoverable from the VM's own config, and it
is the one that matters most: a repository whose password is lost is a
repository nobody can read, including you. Losing it turns every nightly backup
you have ever taken into bytes nobody can open. Put it in the password manager
you would use for a root password.

### Choosing the restic repository

`--restic-repository` has no default, and picking it is a real decision rather
than a formality. The two answers cover different failures.

- **A local path**, for example `/srv/backups/waku`, protects against a bad
  restore, a tenant deleting their own data, and a failed data volume if the
  path is on a different disk. It does not protect against losing the instance:
  the repository dies with the VM.
- **An object store**, for example `s3:s3.amazonaws.com/waku-backups`, protects
  against losing the instance, the disk and the region. It costs a network
  round trip per snapshot, and it needs its own credentials.

Pick the object store unless you have a reason not to. `install.sh` has no flag
for the object store's own credentials: add `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY` to `/srv/waku/config/backup.env` by hand after the
install, following [deploy/backup.env.example](deploy/backup.env.example). A
rerun never overwrites that file, so what you add survives.

**Create the repository yourself, once, before the first backup runs.** Nothing
in this directory runs `restic init`:

```bash
export RESTIC_REPOSITORY=s3:s3.amazonaws.com/waku-backups
export RESTIC_PASSWORD_FILE=/srv/waku/config/restic-password
restic init
```

## Installing

```bash
/srv/waku/src/hosted/deploy/install.sh agent.example.com \
  --dns-provider route53 \
  --acme-email ops@example.com \
  --data-device /dev/sdb1 \
  --free-model claude-sonnet-5 \
  --platform-key-file /root/platform-key \
  --dns-env-file /root/route53-credentials \
  --supabase-url https://abcdefgh.supabase.co \
  --supabase-publishable-key sb_publishable_example \
  --supabase-audience authenticated \
  --restic-repository s3:s3.amazonaws.com/waku-backups \
  --restic-password-file /srv/waku/config/restic-password
```

It runs as root, it is idempotent, and **it never overwrites a config file that
is already there**. A rerun with a different `--free-model` keeps the old value
and says which file it kept. To change a setting after the install, edit the
file under `/srv/waku/config/` and restart that one service.

When it finishes, the apex is `https://agent.example.com` and each tenant is
`https://<id>.agent.example.com`. Caddy asks for the certificate on the first
request to the apex, and that request can take a minute while the DNS-01
challenge propagates.

Delete `/root/platform-key` and `/root/route53-credentials` once the install
has finished. Both values are in `/srv/waku/config/` now, at mode 0600, in a
directory only root can enter.

## What a first deployment does not do yet

Three parts of the design are not in this repository yet, and a deployment made
today runs without them. None of them stops the platform working; each one
changes what you should promise the people using it.

**The free tier does not function, and every tenant must bring their own key.**
The metering proxy is group D of spec 001 and is not written, so `install.sh`
starts the stack with `--scale proxy=0` and prints a warning saying so.
`--free-model` and `--platform-key-file` are still required flags and their
values are still written into the config, but nothing reads them: a tenant
whose Models page is set to the hosted free tier gets a refused connection on
their first turn. Tell your first tenants to add their own Anthropic key on the
Models page. Do not advertise a free tier until group D lands, because an
operator who believes they are offering one and is not will hear about it from
a confused user rather than from a log line.

**There is no spend cap.** The proxy is what meters and caps spending, so with
the proxy scaled to zero nothing counts tokens. This is the same deferral as
the one above, seen from the money side.

**There are no egress rules.** `firewall.sh` is group C of spec 001 and is not
written, so `install.sh` prints a warning and installs no firewall unit. Until
it lands, a tenant's container can reach the VM's private network and the cloud
provider's metadata service. Keep the deployment invite-only, and attach no
instance role or service account to the VM.

## Running it day to day

Every script below lives in `/srv/waku/src/hosted/deploy/` and runs as root on
the VM.

| Command | What it does |
|---|---|
| `tenant.sh status` | the tenant ids with a running container |
| `tenant.sh disable <email or id>` | sets the status, deletes the sessions, revokes the token, stops the container |
| `tenant.sh enable <email or id>` | reverses the status |
| `tenant.sh delete <email or id>` | all of that, then archives the tree and removes the row |
| `tenant.sh inspect <email or id>` | a stock waku dashboard on that tenant's stopped data, on loopback only |
| `tenant.sh inspect-stop <email or id>` | removes that dashboard and lets the tenant start again |
| `upgrade.sh [--ref <git ref>] [--now]` | pulls, rebuilds and restarts the services |
| `backup.sh --all` | every tenant and both platform databases, into restic |
| `restore.sh --tenant <email or id>` | one tenant, from their own snapshot |
| `restore.sh --all` | the whole system onto this VM |
| `migrate.sh --out` / `migrate.sh --in` | move the deployment to another VM |

`tenant.sh` runs `python -m hosted.gateway.admin` inside the gateway's
container. Everything goes through the running gateway, because the gateway
holds the session cache, the container addresses and the tokens in memory: a
second process changing any of that behind its back would leave the gateway
serving from a cache it believes is still true.

### Looking at one tenant's data

`tenant.sh inspect` starts a stock waku dashboard on that tenant's data and
publishes it on the VM's loopback only. Reach it over an SSH tunnel, and the
command prints the exact tunnel to run.

The tenant stays in maintenance for as long as that dashboard exists. Their own
container will not start, and they see a maintenance message. **Run
`tenant.sh inspect-stop <email or id>` when you are done**, or you have taken
somebody's assistant away and left no sign of why.

### Deleting a tenant

`tenant.sh delete` archives the tenant's two directories into
`/srv/waku/archive/<id>/` and prints the two file names. **That archive is the
only copy.** Archives are in no restic snapshot, and the nightly timer deletes
them after 30 days, so the archive is a grace period rather than a backup. Copy
the two files somewhere else if the person may ask for their data back.

`delete` refuses while an inspect container is still running for that tenant,
because the archive it would take is that tenant's only copy and a database
being written to by a live dashboard is a torn one. The refusal arrives after
the tenant has been disabled, so recover with `tenant.sh inspect-stop <id>` and
then either `tenant.sh delete <id>` again or `tenant.sh enable <id>`.

### Backups

A systemd timer runs `backup.sh --all` at 03:17 every night, with up to 15
minutes of random delay, and catches up when the VM was off at that hour.
Restic keeps 7 daily and 4 weekly snapshots per tenant and prunes the rest.

`backup.sh` and `restore.sh` share one `flock` on the staging directory, so a
backup and a restore never run at once. Check on the repository with
`restic snapshots`, using the environment from
`/srv/waku/config/backup.env`.

### Restoring

`restore.sh --tenant <email or id>` restores one person. The gateway stops
their container first; nobody else is interrupted.

`restore.sh --all` restores the whole system in the spec's order: stop every
tenant container, stop the gateway and the proxy, replace `control.db` and
`ledger.db`, start both services, then restore every tenant one at a time. It
checks the control snapshot before it stops anything, so a repository that is
empty or unreachable costs a refusal and no downtime.

Two refusals you may meet, and what each one means:

- *"the gateway did not answer"*. `restore.sh --all` asks the running gateway to
  stop the fleet first. If the gateway is down or crash-looping, which is the
  disaster this command exists for, pass `--no-stop-fleet`. The fleet is still
  stopped once the gateway is back on the restored database, before any tenant
  tree is touched.
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
  run/        the four unix sockets the services talk over
```

Read the logs with `docker compose -p waku logs gateway`, and the same for
`proxy`, `spawner` and `caddy`.

The design behind all of this, and why each choice was made, is
[../docs/architecture.md](../docs/architecture.md) for local waku and the
comments in [deploy/](deploy/) for the deployment.
