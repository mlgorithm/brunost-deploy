# Release readiness

Use this checklist for every Judge deployment. The deployment tool is the
source of truth for rendered Compose/Helm output; the connected Platform
remains a separate application and must be verified through the signed callback
contract.

## Required gates

- `ruff check src tests`, `compileall`, and the complete test suite pass in all
  Judge, Deploy, and Platform Kit repositories (plus any optional Premium
  deployment).
- Judge and worker images, sandbox images, proxy images, PostgreSQL, and object
  storage images are pinned by immutable digest.
- `brunostctl preflight --strict` passes with `.env` and every worker credential
  file mode `0600`.
- Workers have a Docker-compatible runtime, the restricted socket proxy, the
  required versioned seccomp profile, a shared workspace path, and an explicit
  K3s `node_selector` when running on Kubernetes.
- PostgreSQL and object storage are shared and durable for every control-plane
  replica and worker.
- `brunostctl install --dry-run` validates the generated deployment, followed
  by `brunostctl install` with `--atomic`/rollback support.
- `brunostctl verify` reports Judge and Platform ready, signed callbacks, and a
  durable callback dispatcher.
- Exercise worker drain, worker loss, callback retry/replay, backup/restore,
  and rollback before an official round.

## Rollout order

1. Update the topology with digest-pinned images, render it, and snapshot the
   release manifest. `--release` records the immutable topology release; it
   does not select an image tag itself.
2. Deploy or upgrade Judge and workers.
3. Verify Judge `/readyz`, callback dispatcher health, and worker registration.
4. Deploy the Platform and verify Platform `/readyz` plus a signed callback smoke.
5. Keep the previous release snapshot until the contest has passed its first
   operational checkpoint. For Compose, confirm the snapshot contains
   `runtime.env`; it is copied from the previous successful deployment and
   keeps the prior sandbox/proxy runtime pins while secrets continue to come
   from the operator's protected `.env`.
