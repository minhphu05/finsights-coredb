#!/usr/bin/env bash
set -euo pipefail

: "${FINSIGHTS_VPS_HOST:?FINSIGHTS_VPS_HOST is required}"
: "${FINSIGHTS_VPS_PORT:?FINSIGHTS_VPS_PORT is required}"
: "${FINSIGHTS_VPS_USER:?FINSIGHTS_VPS_USER is required}"
: "${FINSIGHTS_VPS_SSH_KEY:?FINSIGHTS_VPS_SSH_KEY is required}"
: "${FINSIGHTS_VPS_KNOWN_HOSTS:?FINSIGHTS_VPS_KNOWN_HOSTS is required}"
: "${MYSQL_NODE_1_HOST:?MYSQL_NODE_1_HOST is required}"
: "${MYSQL_NODE_1_PORT:?MYSQL_NODE_1_PORT is required}"
: "${MYSQL_AUTH_APP_PASSWORD:?MYSQL_AUTH_APP_PASSWORD is required}"
: "${MYSQL_USER_APP_PASSWORD:?MYSQL_USER_APP_PASSWORD is required}"
: "${MYSQL_MARKET_APP_PASSWORD:?MYSQL_MARKET_APP_PASSWORD is required}"

work_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/backend-secret-sync.XXXXXX")"
chmod 700 "$work_dir"
trap 'rm -rf "$work_dir"' EXIT

printf '%s\n' "$FINSIGHTS_VPS_SSH_KEY" > "$work_dir/id_key"
printf '%s\n' "$FINSIGHTS_VPS_KNOWN_HOSTS" > "$work_dir/known_hosts"
chmod 600 "$work_dir/id_key" "$work_dir/known_hosts"

export COREDB_SECRET_PAYLOAD="$work_dir/backend-credentials.json"
python3 - <<'PY'
import json
import os
from pathlib import Path

host = os.environ["MYSQL_NODE_1_HOST"]
port = os.environ["MYSQL_NODE_1_PORT"]
accounts = {
    "AUTH_MYSQL_DSN": ("finsights_auth_app", "MYSQL_AUTH_APP_PASSWORD", "finsights_auth"),
    "USER_MYSQL_DSN": ("finsights_user_app", "MYSQL_USER_APP_PASSWORD", "finsights_user"),
    "MARKET_MYSQL_DSN": ("finsights_market_app", "MYSQL_MARKET_APP_PASSWORD", "finsights_market"),
}

dsns = {}
for key, (username, password_key, database) in accounts.items():
    password = os.environ[password_key]
    if any(char in password for char in "@\r\n"):
        raise SystemExit(f"{password_key} contains a character unsupported by the MySQL DSN format")
    dsns[key] = f"{username}:{password}@tcp({host}:{port})/{database}?parseTime=true&charset=utf8mb4"

payload = Path(os.environ["COREDB_SECRET_PAYLOAD"])
payload.write_text(json.dumps(dsns), encoding="utf-8")
payload.chmod(0o600)
PY

remote="${FINSIGHTS_VPS_USER}@${FINSIGHTS_VPS_HOST}"
remote_dir="/tmp/finsights-backend-secret-sync-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"
ssh_opts=(-i "$work_dir/id_key" -o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$work_dir/known_hosts" -p "$FINSIGHTS_VPS_PORT")
scp_opts=(-i "$work_dir/id_key" -o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$work_dir/known_hosts" -P "$FINSIGHTS_VPS_PORT")

ssh "${ssh_opts[@]}" "$remote" "umask 077 && mkdir -p '$remote_dir'"
scp "${scp_opts[@]}" "$COREDB_SECRET_PAYLOAD" "$remote:$remote_dir/backend-credentials.json"
ssh "${ssh_opts[@]}" "$remote" bash -s -- "$remote_dir" <<'REMOTE'
set -euo pipefail
remote_dir="$1"
trap 'rm -rf "$remote_dir"' EXIT
chmod 600 "$remote_dir/backend-credentials.json"

python3 - "$remote_dir/backend-credentials.json" <<'PY' | kubectl apply -f -
import base64
import json
import subprocess
import sys
from pathlib import Path

name = "finsights-backend-secrets"
namespace = "finsights-platform"
payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
existing = json.loads(subprocess.check_output([
    "kubectl", "-n", namespace, "get", "secret", name, "-o", "json"
], text=True))
data = dict(existing.get("data", {}))
for required in ("JWT_SIGNING_KEY", "RABBITMQ_URL"):
    if not data.get(required):
        raise SystemExit(f"Existing Kubernetes Secret is missing required key: {required}")
for key, value in payload.items():
    data[key] = base64.b64encode(value.encode("utf-8")).decode("ascii")

old_metadata = existing.get("metadata", {})
metadata = {key: old_metadata[key] for key in ("name", "namespace", "labels", "annotations") if key in old_metadata}
secret = {
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": metadata,
    "type": existing.get("type", "Opaque"),
    "data": data,
}
print(json.dumps(secret))
PY

kubectl -n finsights-platform rollout restart \
  deployment/finsights-backend-auth-service \
  deployment/finsights-backend-user-service \
  deployment/finsights-backend-market-service
kubectl -n finsights-platform rollout status deployment/finsights-backend-auth-service --timeout=180s
kubectl -n finsights-platform rollout status deployment/finsights-backend-user-service --timeout=180s
kubectl -n finsights-platform rollout status deployment/finsights-backend-market-service --timeout=180s
REMOTE
