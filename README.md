# Finsights Core DB

`finsights-coredb` initializes and reconciles database objects from versioned YAML and JSON. It creates database names, roles/users, grants, tables, columns, indexes, and foreign keys. It does not insert application records or seed catalog data.

## Targets

| Folder | Server | Managed databases |
|---|---|---|
| `postgres/` | PostgreSQL in the Kubernetes tools stack | `dagster` |
| `mysql-node-1/` | OCI MySQL Tokyo account | `finsights_assets`, `finsights_auth`, `finsights_market`, `finsights_news`, `finsights_user` |
| `mysql-node-2/` | OCI MySQL Singapore account | `finsights_ai` |

The MySQL target configs use the server addresses documented in `finsights-oracle-database`. The workflow reads each node's existing, separately named repository secret so Tokyo and Singapore keep distinct administrator credentials. The endpoint CA certificates are public trust anchors tracked under each MySQL target.

`finsights_market` includes both the canonical market catalog schema and the current backend read model. The market service in `finsights-backend` queries `instruments`, `stock_quotes`, `stock_candles`, `market_indices`, `news_articles`, and `news_article_symbols`. The provided canonical news schema is initialized separately in `finsights_news`; the smaller `news_articles` table in `finsights_market` remains the API projection expected by the current service. The MySQL auth/user schemas remain the current Finsights backend contract. The AI database starts with chat sessions and messages; `user_uid` is a logical identifier, not a cross-server foreign key.

PostgreSQL is reserved for platform tools. The initial managed database is `dagster`, owned by the login role `dagster_admin`; CoreDB does not declare Dagster's internal tables because Dagster creates and migrates those itself. The config transfers ownership of existing tables and sequences in Dagster's `public` schema from `postgres` to `dagster_admin` so the application can keep its current run and event history while using the dedicated login.

The current backend DSNs map to these Node 1 accounts:

| Backend setting | Database | User |
|---|---|---|
| `AUTH_MYSQL_DSN` | `finsights_auth` | `finsights_auth_app` |
| `USER_MYSQL_DSN` | `finsights_user` | `finsights_user_app` |
| `MARKET_MYSQL_DSN` | `finsights_market` | `finsights_market_app` |

Future news/assets services can use `finsights_news` / `finsights_news_app` and `finsights_assets` / `finsights_assets_app`. The AI service account is `finsights_ai_app` on Node 2.

## Reconciliation and removals

The JSON schema files are the desired state. Add or edit a table/column/index there and the workflow reconciles it. Removing a column from a managed table drops that column; removing a table drops that table and its data.

On MySQL, all databases starting with `finsights_` are in this repo's ownership scope. A database removed from that target's `config.yaml` is dropped. Remove its application user from `users` at the same time. Users starting with `finsights_` are also reconciled: a user removed from `users` is dropped. The MySQL account `finsightsadmin` does not match this prefix and is never managed by the workflow. Use the `finsights_` namespace only for objects that this repo owns.

On PostgreSQL, database and role comments mark objects owned by this repo. Removing a marked database or role from `postgres/config.yaml` drops it. Dagster sets `drop_missing_tables: false` and has no schema JSON, so this repo does not prune Dagster's tables.

These drops are deliberate parts of the desired-state workflow. Review schema/config changes before merging them to `main`.

## GitHub Actions setup

The workflow is `.github/workflows/reconcile.yml`. A change under one server folder deploys only that server. Changes to `coredb/`, `requirements.txt`, the deploy script, or the workflow trigger all three targets. `workflow_dispatch` runs all three.

Create these GitHub Environments to scope approvals or deployment protection:

| Environment | Workflow job |
|---|---|
| `coredb-postgres` | PostgreSQL initialization |
| `coredb-mysql-node-1` | Tokyo MySQL initialization |
| `coredb-mysql-node-2` | Singapore MySQL initialization |

Create these **repository secrets** with the exact names below. Existing secrets at repository scope are available to these jobs even though each job selects an Environment:

- PostgreSQL: `POSTGRES_ADMIN_PASSWORD`, `DAGSTER_DB_PASSWORD`.
- MySQL Node 1: `MYSQL_NODE_1_ADMIN_PASSWORD`, `MYSQL_ASSETS_APP_PASSWORD`, `MYSQL_AUTH_APP_PASSWORD`, `MYSQL_MARKET_APP_PASSWORD`, `MYSQL_NEWS_APP_PASSWORD`, `MYSQL_USER_APP_PASSWORD`.
- MySQL Node 2: `MYSQL_NODE_2_ADMIN_PASSWORD`, `MYSQL_AI_APP_PASSWORD`.
- SSH to Node 1: `FINSIGHTS_VPS_SSH_KEY` and `FINSIGHTS_VPS_KNOWN_HOSTS`.

The checked-in MySQL CA certificates are public certificates, not passwords. The current MySQL endpoints present a certificate whose common name is `MySQL_Endpoint_Server` and has no IP subject alternative name. CoreDB verifies the TLS certificate against the per-endpoint CA file and verifies that common name while resolving it to the configured endpoint IP inside the CoreDB process. Refresh the corresponding CA file if OCI rotates that endpoint's certificate chain.

The VPS jobs run sequentially and take a host lock because all three targets execute on Node 1. If Python's `venv` module is absent, `remote-apply.sh` installs only the matching `pythonX.Y-venv` OS package when running as root; it does not install a database service. This keeps the workflow's peak memory use low on the 8 GB VPS.

The workflow runs the reconciler on Node 1 because the GitHub-hosted runner cannot resolve the Kubernetes service DNS or reach the MySQL ingress allowlist directly. Node 1 reaches PostgreSQL through the configured `30432` NodePort and reaches both MySQL endpoints. For PostgreSQL, it also syncs `DAGSTER_DB_PASSWORD` into the Kubernetes secret `orchestration/dagster-admin-postgresql-secret` without logging the value. The Dagster chart uses that secret with `dagster_admin`. The SSH host and port are in the workflow file.

After MySQL Node 1 reconciliation succeeds, the workflow also refreshes the backend `finsights-backend-secrets` values for auth, user, and market DSNs from the same `MYSQL_*_APP_PASSWORD` GitHub secrets used to provision those accounts. It preserves the existing JWT and RabbitMQ values, applies the Secret without printing credentials, and restarts the three database-backed deployments so they read the updated DSNs.

## Local use

For local deployment, the ignored `.env` contains the database passwords. `.env.example` is the tracked template; keep `.env` out of Git. The PostgreSQL default uses Node 1's Tailscale IP and the NodePort from the PostgreSQL chart; set it to a route reachable from your machine if running locally. MySQL uses TLS 1.2 or newer, validates the endpoint-specific CA, and verifies the server certificate name.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m coredb.cli apply --target mysql-node-1
```

Use `--target postgres` or `--target mysql-node-2` for the other servers. The local command reads `.env`; GitHub Actions transfers a short-lived JSON secrets file to Node 1 and removes it after the run.

## PostgreSQL deployment alignment

The Dagster chart uses database `dagster`, login role `dagster_admin`, and the Kubernetes secret synchronized by the PostgreSQL workflow. Dagster owns its internal schema migrations; CoreDB provisions the database and credentials, then leaves table creation and upgrades to Dagster.
