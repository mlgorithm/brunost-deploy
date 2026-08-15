# NREC BGO production baseline

This profile keeps the existing Brunost application untouched while running
the new Platform/Judge stack on `brunost-compute-01`.

- PostgreSQL and MinIO live on `brunost-data-01` in dedicated Judge database and
  object-storage credentials.
- `brunost-worker-cpu-01` and `brunost-worker-cpu-02` are enrolled as separate
  Judge workers.
- The control-plane port is private and restricted to the worker security group.
- The Judge database and artifact bucket have a dedicated daily backup unit on
  the data node.
- The public UI still requires a separately managed HTTPS reverse proxy and DNS
  cutover; this file deliberately does not change the live Brunost edge.

Copy `compose.yml` to the control-plane node and `worker-compose.yml` to every
worker node. Create mode-0600 environment/configuration files and run
`docker compose --env-file .env -f compose.yml config -q` before starting.
