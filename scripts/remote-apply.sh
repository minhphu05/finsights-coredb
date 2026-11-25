#!/usr/bin/env bash
set -euo pipefail

target="${1:?usage: scripts/remote-apply.sh <postgres|mysql-node-1|mysql-node-2>}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

case "$target" in
  postgres)
    secret_keys="POSTGRES_ADMIN_PASSWORD,DAGSTER_DB_PASSWORD"
    optional_keys="POSTGRES_HOST,POSTGRES_PORT,FINSIGHTS_EMBEDDINGS_DB_PASSWORD"
    ;;
  mysql-node-1)
    secret_keys="MYSQL_NODE_1_ADMIN_PASSWORD,MYSQL_AI_APP_PASSWORD"
    optional_keys=""
    ;;
  mysql-node-2)
    secret_keys="MYSQL_NODE_2_ADMIN_PASSWORD,MYSQL_AUTH_APP_PASSWORD,MYSQL_USER_APP_PASSWORD,MYSQL_MARKET_APP_PASSWORD,MYSQL_ASSETS_APP_PASSWORD,MYSQL_NEWS_APP_PASSWORD,MYSQL_SCRAPING_APP_PASSWORD"
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
if [[ "$target" == "mysql-node-2" ]]; then
  .venv/bin/python - "$remote_dir/secrets.json" <<'PY'
import base64
import json
import secrets
import subprocess
import sys
from pathlib import Path

secrets_path = Path(sys.argv[1])
payload = json.loads(secrets_path.read_text(encoding="utf-8"))
result = subprocess.run(
    ["kubectl", "-n", "orchestration", "get", "secret", "dagster-pipeline-secrets", "-o", "json"],
    check=False,
    capture_output=True,
    text=True,
)
password = None
if result.returncode == 0:
    existing = json.loads(result.stdout)
    encoded = existing.get("data", {}).get("MYSQL_AI_RO_PASSWORD")
    if encoded:
        password = base64.b64decode(encoded).decode("utf-8")
if not password:
    password = secrets.token_urlsafe(48)
payload["MYSQL_AI_RO_PASSWORD"] = password
secrets_path.write_text(json.dumps(payload), encoding="utf-8")
secrets_path.chmod(0o600)
PY
fi
if [[ "$target" == "postgres" ]]; then
  .venv/bin/python - "$remote_dir/secrets.json" <<'PY'
import base64
import json
import secrets
import subprocess
import sys
from pathlib import Path

secrets_path = Path(sys.argv[1])
payload = json.loads(secrets_path.read_text(encoding="utf-8"))
password = payload.get("FINSIGHTS_EMBEDDINGS_DB_PASSWORD")
if not password:
    result = subprocess.run(
        ["kubectl", "-n", "orchestration", "get", "secret", "finsights-embeddings-postgresql-secret", "-o", "json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        existing = json.loads(result.stdout)
        encoded = existing.get("data", {}).get("POSTGRES_EMBEDDINGS_PASSWORD")
        if encoded:
            password = base64.b64decode(encoded).decode("utf-8")
if not password:
    password = secrets.token_urlsafe(48)
payload["FINSIGHTS_EMBEDDINGS_DB_PASSWORD"] = password
secrets_path.write_text(json.dumps(payload), encoding="utf-8")
secrets_path.chmod(0o600)
PY
fi
.venv/bin/python -m coredb.cli apply --target "$target" --secrets-file "$remote_dir/secrets.json"
if [[ "$target" == "mysql-node-1" ]]; then
  .venv/bin/python - "$remote_dir/secrets.json" <<'PY' | kubectl -n orchestration apply -f -
import base64
import json
import subprocess
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
password = payload.get("MYSQL_AI_APP_PASSWORD")
if not password:
    raise SystemExit("MYSQL_AI_APP_PASSWORD is required to sync AI chat storage credentials")
name = "dagster-pipeline-secrets"
existing = json.loads(subprocess.check_output([
    "kubectl", "-n", "orchestration", "get", "secret", name, "-o", "json"
], text=True))
data = dict(existing.get("data", {}))
data["MYSQL_AI_APP_PASSWORD"] = base64.b64encode(password.encode("utf-8")).decode("ascii")
old_metadata = existing.get("metadata", {})
metadata = {key: old_metadata[key] for key in ("name", "namespace", "labels", "annotations") if key in old_metadata}
print(json.dumps({
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": metadata,
    "type": existing.get("type", "Opaque"),
    "data": data,
}))
PY
  if kubectl -n orchestration get deployment finsights-ai-api >/dev/null 2>&1; then
    kubectl -n orchestration rollout restart deployment/finsights-ai-api
    kubectl -n orchestration rollout status deployment/finsights-ai-api --timeout=240s
  fi
fi
if [[ "$target" == "mysql-node-2" ]]; then
  .venv/bin/python - "$remote_dir/secrets.json" <<'PY' | kubectl -n orchestration apply -f -
import base64
import json
import subprocess
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
existing = json.loads(subprocess.check_output([
    "kubectl", "-n", "orchestration", "get", "secret", "dagster-pipeline-secrets", "-o", "json"
], text=True))
data = dict(existing.get("data", {}))
data["MYSQL_AI_RO_PASSWORD"] = base64.b64encode(payload["MYSQL_AI_RO_PASSWORD"].encode("utf-8")).decode("ascii")
old_metadata = existing.get("metadata", {})
metadata = {key: old_metadata[key] for key in ("name", "namespace", "labels", "annotations") if key in old_metadata}
print(json.dumps({
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": metadata,
    "type": existing.get("type", "Opaque"),
    "data": data,
}))
PY
fi
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

  embeddings_secret_path="$remote_dir/finsights-embeddings-password"
  .venv/bin/python - "$remote_dir/secrets.json" "$embeddings_secret_path" <<'PY'
import json
import sys
from pathlib import Path

secrets = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
secret_path = Path(sys.argv[2])
secret_path.write_text(secrets["FINSIGHTS_EMBEDDINGS_DB_PASSWORD"], encoding="utf-8")
secret_path.chmod(0o600)
PY
  kubectl -n orchestration create secret generic finsights-embeddings-postgresql-secret \
    --from-file=POSTGRES_EMBEDDINGS_PASSWORD="$embeddings_secret_path" \
    --dry-run=client -o yaml | kubectl apply -f -
  rm -f "$embeddings_secret_path"
fi
REMOTE
