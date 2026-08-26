#!/usr/bin/env bash
set -euo pipefail

# Dedicated Judge backup. The existing Brunost data backup remains separate.
env_file="${BRUNOST_DATA_ENV:-/srv/brunost/env/data.env}"
backup_root="${BRUNOST_JUDGE_BACKUP_DIR:-/srv/brunost/backups/judge}"
retention_days="${BRUNOST_JUDGE_BACKUP_RETENTION_DAYS:-30}"
judge_db="${BRUNOST_JUDGE_DB_NAME:-brunost_judge}"
judge_bucket="${BRUNOST_JUDGE_BUCKET:-brunost-judge-artifacts}"
mc_image="${BRUNOST_JUDGE_MC_IMAGE:?set a digest-pinned MinIO mc image}"

if [[ ! -f "$env_file" || -z "$backup_root" || "$backup_root" == "/" ]]; then
  echo "invalid backup configuration" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$env_file"
set +a

project="${COMPOSE_PROJECT_NAME:-brunost-data}"
postgres_container="${BRUNOST_POSTGRES_CONTAINER:-${project}-postgres-1}"
network="${BRUNOST_DATA_DOCKER_NETWORK:-${project}_default}"
timestamp="$(date -u +%Y-%m-%dT%H%M%SZ)"
backup_dir="$backup_root/$timestamp"
work_dir="$backup_root/.$timestamp.tmp"
lock_file="${BRUNOST_JUDGE_BACKUP_LOCK_FILE:-/tmp/brunost-judge-backup.lock}"

required_vars=(POSTGRES_USER POSTGRES_PASSWORD MINIO_ROOT_USER MINIO_ROOT_PASSWORD)
for name in "${required_vars[@]}"; do
  [[ -n "${!name:-}" ]] || { echo "$name is required in $env_file" >&2; exit 2; }
done

umask 077
mkdir -p "$backup_root"
exec 9>"$lock_file"
flock -n 9 || { echo "another Judge backup is running" >&2; exit 1; }
rm -rf "$work_dir"
mkdir -p "$work_dir/objects"
trap 'rm -rf "$work_dir"' ERR INT TERM

docker exec -e "PGPASSWORD=$POSTGRES_PASSWORD" "$postgres_container" \
  pg_dump -U "$POSTGRES_USER" -d "$judge_db" --format=custom --no-owner \
  > "$work_dir/postgres.dump"

docker run --rm --network "$network" --entrypoint /bin/sh \
  -v "$work_dir/objects:/backup/objects" \
  -e "MINIO_ROOT_USER=$MINIO_ROOT_USER" \
  -e "MINIO_ROOT_PASSWORD=$MINIO_ROOT_PASSWORD" \
  -e "JUDGE_BUCKET=$judge_bucket" "$mc_image" -eu -c '
    mc alias set data http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
    mkdir -p /backup/objects/artifacts
    mc mirror --overwrite "data/$JUDGE_BUCKET" /backup/objects/artifacts
  '

(cd "$work_dir" && sha256sum postgres.dump > SHA256SUMS)
cat > "$work_dir/manifest.json" <<JSON
{
  "created_at": "$timestamp",
  "database": "$judge_db",
  "artifact_bucket": "$judge_bucket",
  "postgres_dump": "postgres.dump"
}
JSON
(cd "$work_dir" && find . -type f ! -name 'SHA256SUMS.all' -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS.all)
mv "$work_dir" "$backup_dir"
ln -sfn "$backup_dir" "$backup_root/latest"

if [[ "$retention_days" =~ ^[0-9]+$ && "$retention_days" -gt 0 ]]; then
  find "$backup_root" -mindepth 1 -maxdepth 1 -type d -name '20*Z' -mtime "+$retention_days" -exec rm -rf {} +
fi
echo "Judge backup complete: $backup_dir"
