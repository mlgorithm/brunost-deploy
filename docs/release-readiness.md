# Release readiness

Use this checklist for every Judge deployment. The deployment tool is the
source of truth for rendered Compose/Helm output; Premium remains a separate
application and must be verified through the signed callback contract.

## Required gates

- `ruff check src tests`, `compileall`, and the complete test suite pass in all
  four repositories: Judge, Deploy, Platform Kit, and Premium.
- Judge and worker images, sandbox images, proxy images, PostgreSQL, and object
  storage images are pinned by immutable digest.
- `brunostctl preflight --strict` passes with `.env` and every worker credential
  file mode `0600`.
- Workers have a Docker-compatible runtime, the restricted socket proxy, the
  required seccomp profile, and a shared workspace path.
- PostgreSQL and object storage are shared and durable for every control-plane
  replica and worker.
- `brunostctl install --dry-run` validates the generated deployment, followed
  by `brunostctl install` with `--atomic`/rollback support.
- `brunostctl verify` reports Judge and Premium ready, signed callbacks, and a
  durable callback dispatcher.
- Exercise worker drain, worker loss, callback retry/replay, backup/restore,
  and rollback before an official round.

## Rollout order

1. Render and snapshot the release manifest.
2. Deploy or upgrade Judge and workers.
3. Verify Judge `/readyz`, callback dispatcher health, and worker registration.
4. Deploy Premium and verify Premium `/readyz` plus a signed callback smoke.
5. Keep the previous release snapshot until the contest has passed its first
   operational checkpoint.
