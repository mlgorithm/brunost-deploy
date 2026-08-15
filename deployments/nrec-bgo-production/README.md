# NREC BGO production baseline

This profile keeps the existing Brunost application untouched while running
the new Platform/Judge stack on `brunost-compute-01`.

- PostgreSQL and MinIO live on `brunost-data-01` in dedicated Judge database and
  object-storage credentials.
- `brunost-worker-cpu-01` and `brunost-worker-cpu-02` are enrolled as separate
  Judge workers.
- The control-plane port is private and restricted to the worker security group.
- The platform callback endpoint is bound to the compute node's private address
  and restricted to the worker security group; worker `.env` files must include
  the same callback-signing secret as the Judge API.
- The Judge database and artifact bucket have a dedicated daily backup unit on
  the data node.
- The public UI still requires a separately managed HTTPS reverse proxy and DNS
  cutover; this file deliberately does not change the live Brunost edge.

Copy `compose.yml` to the control-plane node and `worker-compose.yml` to every
worker node. Create mode-0600 environment/configuration files and run
`docker compose --env-file .env -f compose.yml config -q` before starting.

For a distributed installation, set these control-plane values (replace the
private address with the node address in your topology):

```dotenv
BRUNOST_PLATFORM_BIND=0.0.0.0
BRUNOST_PLATFORM_CALLBACK_URL=http://10.0.0.10:3000/api/judge/callback
BRUNOST_JUDGE_CALLBACK_HOSTS=10.0.0.10
```

Allow TCP 3000 only from the worker security group. Do not expose this port to
the public network; public users should arrive through the separately managed
HTTPS edge. Every worker must have `BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET`
set to the same value as the control plane and must be able to reach the
private callback address.
