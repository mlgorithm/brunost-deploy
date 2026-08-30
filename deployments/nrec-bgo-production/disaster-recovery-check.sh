#!/usr/bin/env bash
set -euo pipefail

backup_dir="${1:-${BRUNOST_JUDGE_BACKUP_DIR:-/srv/brunost/backups/judge/latest}}"
if [[ ! -d "$backup_dir" || ! -f "$backup_dir/postgres.dump" || ! -f "$backup_dir/SHA256SUMS.all" ]]; then
  echo "backup directory is incomplete: $backup_dir" >&2
  exit 2
fi
if ! command -v sha256sum >/dev/null || ! command -v pg_restore >/dev/null; then
  echo "sha256sum and pg_restore are required for a DR check" >&2
  exit 2
fi
(cd "$backup_dir" && sha256sum -c SHA256SUMS.all)
pg_restore --list "$backup_dir/postgres.dump" >/dev/null
printf '%s\n' "DR backup check passed: $backup_dir" "Next: restore into an isolated PostgreSQL and object-store target, run brunostctl verify, then perform the Premium submission/callback smoke test."
