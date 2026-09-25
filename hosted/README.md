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
the Waku mark and the Waku names are not: they are listed in
[LICENSE-BRAND](../LICENSE-BRAND), and a hosted deployment serves the stock dashboard, which carries them. **A
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

## Running it

Hosted waku runs on **one Ubuntu 24.04 VM**. Everything below is done once, in
this order. The example domain here is the deployment this was built for,
`agent.waku.one`; substitute your own.

### 1. What to create at your provider, by hand

Spec 001 does not automate this layer (design section 11), and `install.sh`
refuses to run until it is right.

| Create | Notes |
|---|---|
| A VM | 4 vCPU, 16 GB RAM to start. Memory is the binding resource: about 100 MB per active tenant, and `install.sh` sizes its running cap from it as (memory minus 2 GB) divided by 150 MB, which is 95 on a 16 GB VM. Pass `--max-running N` to choose your own number. Idle containers stop on their own, so the cap limits how many tenants run at once and not how many can sign up |
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
and `upgrade.sh --ref v0.4.0` pins a tag. Installing from `main` means
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
`--dns-module-version @v1.5.0` if you want a rebuild next month to produce the
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
| The platform's model key | `--platform-key-file` | Copied into `/srv/waku/config/proxy.env` at mode 0600. Delete the source file afterwards |
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

### 7. Install

```bash
sudo /srv/waku/src/hosted/deploy/install.sh agent.waku.one \
  --dns-provider route53 \
  --dns-env-file /root/route53-credentials \
  --dns-env AWS_REGION=us-east-1 \
  --acme-email ops@example.com \
  --data-device /dev/nvme1n1 \
  --free-model claude-haiku-4-5 \
  --platform-key-file /root/platform-key \
  --supabase-url https://<project>.supabase.co \
  --supabase-publishable-key sb_publishable_... \
  --supabase-audience <the aud claim your project's tokens carry> \
  --restic-repository s3:s3.amazonaws.com/<bucket> \
  --restic-password-file /srv/waku/config/restic-password
```

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

## What is not enabled yet

Two pieces of spec 001 are deferred, and this deployment is invite-only because
of them.

**No metering proxy, so no free tier and no spend cap.** Group D is not built,
so the `proxy` service is declared and started with zero replicas. `--free-model`
and `--platform-key-file` are still required flags and their values are still
written into the config, but nothing reads them: a tenant whose Models page is
set to the hosted free tier gets a refused connection on their first turn.
**Every tenant must bring their own key**, which is the ordinary provider
switch on the Models page. Do not advertise a free tier until group D lands,
because an operator who believes they are offering one and is not will hear
about it from a confused user rather than from a log line. Nothing counts
tokens either, so there is no cap on what a tenant's own key can spend.

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
sudo du -sh /srv/waku/tenants/<id> /srv/waku/archive/<id>
sudo rm -rf /srv/waku/tenants/<id>                     # the archive stays
```

`delete` refuses while an inspect container is still running for that tenant,
because the archive it would take is that tenant's only copy and a database
being written to by a live dashboard is a torn one. The refusal arrives after
the tenant has been disabled, so recover with `tenant.sh inspect-stop <id>` and
then either `tenant.sh delete <id>` again or `tenant.sh enable <id>`.

### Backups

A systemd timer runs `backup.sh --all` at 03:17 every night, with up to 15
minutes of random delay, and catches up when the VM was off at that hour.
Restic keeps 7 daily and 4 weekly snapshots per tenant and prunes the rest.

Every snapshot carries the backup's own `manifest.json`, written last. A
restore refuses a snapshot without one, because a backup that did not finish
cannot be told from an empty tenant by looking.

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
