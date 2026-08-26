# NREC BGO production baseline

This profile runs the distributed Judge stack on `brunost-compute-01` and
supports Premium as a separately managed private application. It does not
deploy the Premium API or any Premium database.

- PostgreSQL and MinIO live on `brunost-data-01` in dedicated Judge database and
  object-storage credentials.
- `brunost-worker-cpu-01` and `brunost-worker-cpu-02` are enrolled as separate
  Judge workers.
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

## Premium cutover

Configure Premium's `BRUNOST_JUDGE_URL`, `BRUNOST_JUDGE_API_TOKEN`,
`BRUNOST_JUDGE_CALLBACK_URL`, `BRUNOST_JUDGE_CALLBACK_TOKEN`, and
`BRUNOST_JUDGE_CALLBACK_SECRET` from the same deployment secret store. Verify
Premium `/readyz`, Judge `/healthz`, one immutable artifact upload, one
submission, and one signed callback before enabling contest traffic.

## Current NREC layout

The production deployment currently runs the following roles:

| Node | Role |
| --- | --- |
| `brunost-app-01` | Public Nginx edge, Premium API/worker, Platform Core, monitoring |
| `brunost-app-02` | Private Premium API/worker and Platform Core replica |
| `brunost-data-01` | PostgreSQL primary, Redis, and MinIO |
| `brunost-data-02` | PostgreSQL asynchronous streaming replica |
| `brunost-data-03` | PostgreSQL asynchronous streaming replica |
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
