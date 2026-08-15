# NREC BGO production baseline

This profile runs the new Platform/Judge stack on `brunost-compute-01` and
supports the live `brunost.ioai.no` edge cutover without moving TLS
termination.

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
- The live `brunost.ioai.no` edge keeps HTTPS termination on the existing Nginx
  node and proxies to the new Platform address. Stop the old Brunost
  frontend/backend/worker containers only after the public smoke test passes.

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

## Live edge cutover

For `brunost.ioai.no`, keep TLS termination on the existing Nginx edge and
replace the application upstream with the Platform address:

```nginx
upstream brunost_platform {
    server 10.0.0.10:3000 max_fails=2 fail_timeout=10s;
}

location / {
    proxy_pass http://brunost_platform;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
}
```

Allow the edge security group to reach TCP 3000 on the compute security group.
Validate `/healthz`, `/login`, administrator access, and one real submission
through the public hostname. Keep the previous Nginx file as a rollback copy
until the new system has been observed successfully.
