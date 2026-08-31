# NREC BGO production baseline

This profile runs the distributed Judge stack on `brunost-compute-01` and
supports Premium as a separately managed private application. It does not
deploy the Premium API or any Premium database.

- PostgreSQL and MinIO live on `brunost-data-01` in dedicated Judge database and
  object-storage credentials.
- `brunost-worker-cpu-01` and `brunost-worker-cpu-02` are enrolled as separate
  Judge workers.
- the Judge control plane runs a durable callback dispatcher so a worker loss
  after finishing an execution cannot lose the Premium result notification.
- The control-plane port is private and restricted to the worker security group.
- Judge callback delivery is allowlisted to the Premium callback hostname; the
  Premium API owns the callback endpoint and worker `.env` files must include
  the same callback-signing secret as the Judge API.
- The Judge database and artifact bucket have a dedicated daily backup unit on
  the data node.
- Premium keeps its own HTTPS edge and points at this Judge API through the
  configured service URL. Stop the old Judge workers only after the Premium
  submission smoke test passes.

Copy `compose.yml` to the control-plane node and `worker-compose.yml` to every
worker node. Create mode-0600 environment/configuration files and run
`docker compose --env-file .env -f compose.yml config -q` before starting.
The Compose files use a 15-minute worker stop grace period; run the drain API
command before stopping a worker so leased work can finish.

For a distributed installation, set these control-plane values (replace the
private address with the node address in your topology):

```dotenv
BRUNOST_JUDGE_CALLBACK_HOSTS=premium.example.org
```

Keep the Judge API private behind the Premium/network edge and allow Premium to
reach TCP 8787. Every worker must have
`BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET` set to the same value as the control
plane. Premium must use the same value as
`BRUNOST_JUDGE_CALLBACK_SECRET` and configure
`BRUNOST_JUDGE_CALLBACK_URL=https://premium.example.org/api/judge/callback`.

## Backup and recovery commands

Run the daily unit after installing the executable scripts under
`/usr/local/sbin`:

```bash
install -m 0755 backup-judge.sh /usr/local/sbin/brunost-judge-backup
install -m 0755 restore-judge.sh /usr/local/sbin/brunost-judge-restore
install -m 0755 disaster-recovery-check.sh /usr/local/sbin/brunost-judge-dr-check
systemctl enable --now brunost-judge-backup.timer
/usr/local/sbin/brunost-judge-dr-check /srv/brunost/backups/judge/latest
```

The backup contains a custom-format PostgreSQL dump, mirrored Judge artifact
objects, a manifest, and checksums. Restore only into the intended target and
only with an explicit confirmation:

```bash
BRUNOST_RESTORE_CONFIRM=YES \
  /usr/local/sbin/brunost-judge-restore /srv/brunost/backups/judge/2026-08-30T033500Z
```

Use the isolated recovery procedure in [DR.md](DR.md) for a periodic drill.
The scripts do not promote asynchronous PostgreSQL replicas or change DNS.

## Premium cutover

Configure Premium's `BRUNOST_JUDGE_URL`, `BRUNOST_JUDGE_API_TOKEN`,
`BRUNOST_JUDGE_CALLBACK_URL`, `BRUNOST_JUDGE_CALLBACK_TOKEN`, and
`BRUNOST_JUDGE_CALLBACK_SECRET` from the same deployment secret store. Verify
Premium `/readyz` and Judge `/readyz` before enabling contest traffic. If this
profile is operated from a `brunostctl` topology bundle, run its read-only
verification first:

```bash
brunostctl verify --config brunost.yaml \
  --url "$BRUNOST_JUDGE_URL" --token "$BRUNOST_JUDGE_API_TOKEN" \
  --premium-url "$BRUNOST_PREMIUM_URL"
```

The command checks the durable callback-dispatcher wiring and readiness only;
it does not create submissions or replay callbacks. After that check, perform
the separate controlled smoke test for one immutable artifact, one
submission, and one signed callback before enabling contest traffic.

The version-controlled public edge baseline is `premium-edge.conf`. Before
installing it, preserve the active config under `/srv/brunost/nginx-backups/`
(never in `sites-enabled`), confirm every upstream address against the live
OpenStack inventory, run `nginx -t`, and reload only after the test passes.

## Current NREC layout

The production deployment currently runs the following roles:

| Node | Role |
| --- | --- |
| `brunost-app-01` | Public Nginx edge, Premium API/worker, Platform Core, monitoring |
| `brunost-app-02` (`158.39.201.29`) | Private Premium API/worker and Platform Core replica |
| `brunost-data-01` | PostgreSQL primary, Redis, and MinIO |
| `brunost-data-02` (`158.39.201.74`) | PostgreSQL asynchronous streaming replica |
| `brunost-data-03` (`158.39.201.226`) | PostgreSQL asynchronous streaming replica |
| `brunost-compute-01` | Judge control plane |
| `brunost-worker-cpu-01`, `brunost-worker-cpu-02` | Distributed Judge workers |
| `brunost-notebook-01` | Notebook/lab workloads |

Nginx sends root, Premium, and Platform Core traffic to both app nodes over
the private network. Judge traffic remains on the dedicated control plane.
The legacy Brunost frontend/backend/beat containers are retired from the
production runtime; their images and dated Nginx/environment backups remain
only for the rollback window.

This is active-active at the application layer, but not automatic end-to-end
failover yet: NREC does not provide a managed load balancer in this project,
so the public edge is still `brunost-app-01`; PostgreSQL replicas are
asynchronous and require an operator-led promotion; Redis and MinIO remain on
`brunost-data-01`. Do not describe this profile as automatic HA until those
three failover paths are separately automated and tested.

Before removing rollback artifacts, verify the public Premium submission and
signed Judge callback flow, both app-node health checks, and two streaming
replicas. Keep the PostgreSQL base-backup procedure and the dated app/Nginx
backups available for recovery.

## Premium `ab03bc7` release evidence

The authoritative deterministic release task is:

```text
release-testing/premium-ab03bc7-deterministic-sum-v2
```

- Standalone Judge execution `c2f5c17a-12d2-41b0-a56e-365df0bb5a14`
  completed with score `1.0`.
- Premium submission `444910be-0f5d-4b99-b616-f88c43faa955` produced Judge
  execution `e89c01ce-b8bc-499c-b321-4960e58e3b02`, completed with score `1.0`,
  and applied its signed callback to contest progress.

`release-testing/premium-ab03bc7-deterministic-sum-v1` is **invalid release
evidence**. Its uploaded archive contained macOS AppleDouble entries, so execution
`711c6851-2429-408a-b4db-fd627d62410b` scored `0.0`. Judge task references and
executions are intentionally immutable and have no deletion API; retain this
record as audit evidence rather than modifying the Judge database. Release gates
must allowlist the exact `-v2` reference above and reject the `-v1` reference.

Build future Judge archives on Linux or disable macOS copyfile metadata, and list
the archive before upload. A release archive containing `._*` entries must fail
preflight rather than be registered under a task reference.
