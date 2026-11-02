#!/usr/bin/env bash
set -euo pipefail

target="${1:?usage: scripts/remote-apply.sh <postgres|mysql-node-1|mysql-node-2>}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

case "$target" in
  postgres)
    secret_keys="POSTGRES_ADMIN_PASSWORD,DAGSTER_DB_PASSWORD"
    optional_keys="POSTGRES_HOST,POSTGRES_PORT"
    ;;
  mysql-node-1)
    secret_keys="MYSQL_NODE_1_ADMIN_PASSWORD,MYSQL_ASSETS_APP_PASSWORD,MYSQL_AUTH_APP_PASSWORD,MYSQL_MARKET_APP_PASSWORD,MYSQL_NEWS_APP_PASSWORD,MYSQL_USER_APP_PASSWORD"
    optional_keys=""
    ;;
  mysql-node-2)
    secret_keys="MYSQL_NODE_2_ADMIN_PASSWORD,MYSQL_AI_APP_PASSWORD"
    optional_keys=""
    ;;
  *)
    echo "Unsupported database target: $target" >&2
    exit 2
    ;;
esac

: "${FINSIGHTS_VPS_HOST:?FINSIGHTS_VPS_HOST is required}"
: "${FINSIGHTS_VPS_PORT:?FINSIGHTS_VPS_PORT is required}"
: "${FINSIGHTS_VPS_USER:?FINSIGHTS_VPS_USER is required}"
: "${FINSIGHTS_VPS_SSH_KEY:?FINSIGHTS_VPS_SSH_KEY is required}"
: "${FINSIGHTS_VPS_KNOWN_HOSTS:?FINSIGHTS_VPS_KNOWN_HOSTS is required}"

work_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/coredb-${target}.XXXXXX")"
chmod 700 "$work_dir"
trap 'rm -rf "$work_dir"' EXIT

printf '%s\n' "$FINSIGHTS_VPS_SSH_KEY" > "$work_dir/id_key"
printf '%s\n' "$FINSIGHTS_VPS_KNOWN_HOSTS" > "$work_dir/known_hosts"
chmod 600 "$work_dir/id_key" "$work_dir/known_hosts"

export COREDB_SECRET_KEYS="$secret_keys"
export COREDB_OPTIONAL_KEYS="$optional_keys"
export COREDB_SECRET_OUTPUT="$work_dir/secrets.json"
python3 - <<'PY'
import json
import os
from pathlib import Path

keys = [key for key in os.environ["COREDB_SECRET_KEYS"].split(",") if key]
optional = [key for key in os.environ.get("COREDB_OPTIONAL_KEYS", "").split(",") if key]
missing = [key for key in keys if not os.environ.get(key)]
if missing:
    raise SystemExit("Missing required GitHub secrets: " + ", ".join(missing))

output = Path(os.environ["COREDB_SECRET_OUTPUT"])
output.write_text(json.dumps({key: os.environ[key] for key in keys + optional if key in os.environ}), encoding="utf-8")
output.chmod(0o600)
PY

tar -C "$repo_root" -czf "$work_dir/repo.tgz" coredb requirements.txt "$target"
remote="${FINSIGHTS_VPS_USER}@${FINSIGHTS_VPS_HOST}"
remote_dir="/tmp/finsights-coredb-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}-${target}"
ssh_opts=(-i "$work_dir/id_key" -o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$work_dir/known_hosts" -p "$FINSIGHTS_VPS_PORT")
scp_opts=(-i "$work_dir/id_key" -o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$work_dir/known_hosts" -P "$FINSIGHTS_VPS_PORT")

ssh "${ssh_opts[@]}" "$remote" "umask 077 && mkdir -p '$remote_dir'"
scp "${scp_opts[@]}" "$work_dir/repo.tgz" "$work_dir/secrets.json" "$remote:$remote_dir/"
ssh "${ssh_opts[@]}" "$remote" bash -s -- "$remote_dir" "$target" <<'REMOTE'
set -euo pipefail
remote_dir="$1"
target="$2"
trap 'rm -rf "$remote_dir"' EXIT
exec 9>/var/lock/finsights-coredb.lock
flock 9
chmod 600 "$remote_dir/secrets.json"
tar -xzf "$remote_dir/repo.tgz" -C "$remote_dir"
cd "$remote_dir"
if ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
  if ! command -v apt-get >/dev/null 2>&1 || [[ "$(id -u)" -ne 0 ]]; then
    echo "Python venv support is missing; install the matching pythonX.Y-venv package as root." >&2
    exit 1
  fi
  python_version="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y "python${python_version}-venv"
fi
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check --quiet -r requirements.txt
.venv/bin/python -m coredb.cli apply --target "$target" --secrets-file "$remote_dir/secrets.json"
if [[ "$target" == "postgres" ]]; then
  secret_path="$remote_dir/dagster-postgresql-password"
  .venv/bin/python - "$remote_dir/secrets.json" "$secret_path" <<'PY'
import json
import os
import sys
from pathlib import Path

secrets = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
password = secrets["DAGSTER_DB_PASSWORD"]
secret_path = Path(sys.argv[2])
secret_path.write_text(password, encoding="utf-8")
secret_path.chmod(0o600)
PY
  kubectl -n orchestration create secret generic dagster-admin-postgresql-secret \
    --from-file=postgresql-password="$secret_path" \
    --dry-run=client -o yaml | kubectl apply -f -
  rm -f "$secret_path"
fi
REMOTE
