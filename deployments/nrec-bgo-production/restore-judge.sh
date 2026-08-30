#!/usr/bin/env bash
set -euo pipefail

# Restore one backup created by backup-judge.sh. This is intentionally
# destructive and requires an explicit confirmation in the environment.
backup_dir="${1:-}"
env_file="${BRUNOST_DATA_ENV:-/srv/brunost/env/data.env}"
judge_db="${BRUNOST_JUDGE_DB_NAME:-brunost_judge}"
judge_bucket="${BRUNOST_JUDGE_BUCKET:-brunost-judge-artifacts}"
mc_image="${BRUNOST_JUDGE_MC_IMAGE:?set a digest-pinned MinIO mc image}"

if [[ -z "$backup_dir" || ! -d "$backup_dir" || ! -f "$backup_dir/postgres.dump" ]]; then
  echo "usage: restore-judge.sh /path/to/timestamped-backup" >&2
  exit 2
fi
if [[ "${BRUNOST_RESTORE_CONFIRM:-}" != "YES" ]]; then
  echo "refusing destructive restore; set BRUNOST_RESTORE_CONFIRM=YES" >&2
  exit 2
fi
if [[ ! "$mc_image" =~ @sha256:[0-9a-fA-F]{64}$ ]]; then
  echo "BRUNOST_JUDGE_MC_IMAGE must be pinned by a sha256 digest" >&2
  exit 2
fi
if [[ ! -f "$env_file" ]]; then
  echo "missing data environment: $env_file" >&2
  exit 2
fi
if ! command -v docker >/dev/null || ! command -v sha256sum >/dev/null; then
  echo "docker and sha256sum are required" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$env_file"
set +a
project="${COMPOSE_PROJECT_NAME:-brunost-data}"
postgres_container="${BRUNOST_POSTGRES_CONTAINER:-${project}-postgres-1}"
network="${BRUNOST_DATA_DOCKER_NETWORK:-${project}_default}"
required_vars=(POSTGRES_USER POSTGRES_PASSWORD MINIO_ROOT_USER MINIO_ROOT_PASSWORD)
for name in "${required_vars[@]}"; do
  [[ -n "${!name:-}" ]] || { echo "$name is required in $env_file" >&2; exit 2; }
done

(cd "$backup_dir" && sha256sum -c SHA256SUMS.all)
docker exec -i "$postgres_container" pg_restore --list < "$backup_dir/postgres.dump" >/dev/null
docker exec -e "PGPASSWORD=$POSTGRES_PASSWORD" -i "$postgres_container" \
  pg_restore -U "$POSTGRES_USER" -d "$judge_db" --clean --if-exists --no-owner --exit-on-error \
  < "$backup_dir/postgres.dump"

docker run --rm --network "$network" --entrypoint /bin/sh \
  -v "$backup_dir/objects:/backup/objects:ro" \
  -e "MINIO_ROOT_USER=$MINIO_ROOT_USER" \
  -e "MINIO_ROOT_PASSWORD=$MINIO_ROOT_PASSWORD" \
  -e "JUDGE_BUCKET=$judge_bucket" "$mc_image" -eu -c '
    mc alias set data http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
    mc mb --ignore-existing "data/$JUDGE_BUCKET"
    mc mirror --overwrite /backup/objects/artifacts "data/$JUDGE_BUCKET"
  '
echo "Judge restore complete: $backup_dir"
