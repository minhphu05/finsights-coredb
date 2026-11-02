# Finsights Core DB

`finsights-coredb` initializes and reconciles database objects from versioned YAML and JSON. It creates database names, roles/users, grants, tables, columns, indexes, and foreign keys. It does not insert application records or seed catalog data.

## Targets

| Folder | Server | Managed databases |
|---|---|---|
| `postgres/` | PostgreSQL in the Kubernetes tools stack | `dagster`, `auth_service`, `user_service` |
| `mysql-node-1/` | OCI MySQL Tokyo account | `finsights_assets`, `finsights_auth`, `finsights_market`, `finsights_news`, `finsights_user` |
| `mysql-node-2/` | OCI MySQL Singapore account | `finsights_ai` |

The MySQL target configs use the server addresses documented in `finsights-oracle-database`. Each GitHub Environment supplies its own `MYSQL_ADMIN_PASSWORD`, so Tokyo and Singapore can keep separate administrator credentials.

`finsights_market` includes both the canonical market catalog schema and the current backend read model. The market service in `finsights-backend` queries `instruments`, `stock_quotes`, `stock_candles`, `market_indices`, `news_articles`, and `news_article_symbols`. The provided canonical news schema is initialized separately in `finsights_news`; the smaller `news_articles` table in `finsights_market` remains the API projection expected by the current service. The MySQL auth/user schemas remain the current Finsights backend contract. Separately, the PostgreSQL `auth_service` and `user_service` databases mirror the Prisma models in `Fizzisme/fiurozz-be` branch `develop`; they do not replace the MySQL databases. The AI database starts with chat sessions and messages; `user_uid` is a logical identifier, not a cross-server foreign key.

`postgres/schemas/auth-service.json` mirrors [`auth-service/prisma/schema.prisma`](https://github.com/Fizzisme/fiurozz-be/blob/develop/auth-service/prisma/schema.prisma), and `postgres/schemas/user-service.json` mirrors [`user-service/prisma/schema.prisma`](https://github.com/Fizzisme/fiurozz-be/blob/develop/user-service/prisma/schema.prisma) from `Fizzisme/fiurozz-be` branch `develop`. They define the service tables, PostgreSQL enums, UUID relationships, indexes, and cascade deletes. The databases are owned by `auth_service_app` and `user_service_app`, with separate passwords. `dagster` remains dedicated to Dagster and is not schema-managed by CoreDB.

| Service | PostgreSQL database | Login role | GitHub Environment secret |
|---|---|---|---|
| auth-service | `auth_service` | `auth_service_app` | `AUTH_SERVICE_DB_PASSWORD` |
| user-service | `user_service` | `user_service_app` | `USER_SERVICE_DB_PASSWORD` |

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

On PostgreSQL, database and role comments mark objects owned by this repo. Removing a marked database or role from `postgres/config.yaml` drops it; remove a service database and its role together. The service database schemas set `drop_missing_tables: true`, so removing a table from their JSON drops it and its rows. Removing a column also drops its data. Dagster sets `drop_missing_tables: false` and has no schema JSON, so this repo does not prune tables inside `dagster`.

These drops are deliberate parts of the desired-state workflow. Review schema/config changes before merging them to `main`.

## GitHub Actions setup

The workflow is `.github/workflows/reconcile.yml`. A change under one server folder deploys only that server. Changes to `coredb/`, `requirements.txt`, the deploy script, or the workflow trigger all three targets. `workflow_dispatch` runs all three.

Create these GitHub Environments:

| Environment | Required environment secrets |
|---|---|
| `coredb-postgres` | `POSTGRES_ADMIN_PASSWORD`, `DAGSTER_DB_PASSWORD`, `AUTH_SERVICE_DB_PASSWORD`, `USER_SERVICE_DB_PASSWORD` |
| `coredb-mysql-node-1` | `MYSQL_ADMIN_PASSWORD`, `MYSQL_TLS_CA_PEM`, `MYSQL_ASSETS_APP_PASSWORD`, `MYSQL_AUTH_APP_PASSWORD`, `MYSQL_MARKET_APP_PASSWORD`, `MYSQL_NEWS_APP_PASSWORD`, `MYSQL_USER_APP_PASSWORD` |
| `coredb-mysql-node-2` | `MYSQL_ADMIN_PASSWORD`, `MYSQL_TLS_CA_PEM`, `MYSQL_AI_APP_PASSWORD` |

Set a different `MYSQL_ADMIN_PASSWORD` and the matching trusted `MYSQL_TLS_CA_PEM` value in each MySQL Environment. Add the optional environment variable `MYSQL_HOST` if the MySQL certificate is issued to a DNS name instead of the configured public IP; that name must resolve to the same MySQL endpoint. Add these repository secrets for the SSH connection to Node 1:

- `FINSIGHTS_VPS_SSH_KEY`: the private key accepted by the Node 1 SSH service.
- `FINSIGHTS_VPS_KNOWN_HOSTS`: the verified `known_hosts` entry for `103.249.117.232` on port `26567`.

The workflow runs the reconciler on Node 1 because the GitHub-hosted runner cannot resolve the Kubernetes service DNS or reach the MySQL ingress allowlist directly. Node 1 reaches PostgreSQL through the configured `30432` NodePort and reaches both MySQL endpoints. The SSH host and port are in the workflow file.

## Local use

This working copy has an ignored `.env` with generated Dagster, auth-service, user-service, and application-account passwords. Add the existing PostgreSQL/MySQL administrator passwords and the trusted OCI MySQL CA file paths before local deployment. `.env.example` is the tracked template; keep `.env` out of Git. The PostgreSQL default uses Node 1's Tailscale IP and the NodePort from the PostgreSQL chart; set it to a route reachable from your machine if running locally. MySQL connects with TLS 1.2 or newer, CA verification, and hostname verification. If a certificate uses a DNS name, set the corresponding `MYSQL_NODE_1_HOST` or `MYSQL_NODE_2_HOST` to that name.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m coredb.cli apply --target mysql-node-1
```

Use `--target postgres` or `--target mysql-node-2` for the other servers. The local command reads `.env`; GitHub Actions transfers a short-lived JSON secrets file to Node 1 and removes it after the run.

## PostgreSQL deployment alignment

The current Dagster values in `finsights-platform-deployment` still configure `tools_admin`. This repo creates `dagster_admin` as requested. Update the Dagster chart values/secret to use `dagster_admin` and the same `DAGSTER_DB_PASSWORD` before relying on the new credentials; this repo does not edit the deployment repo.

The Fiurozz services currently document `npx prisma migrate deploy`. Do not run that against databases after CoreDB has created these tables: Prisma will see no applied migration history and its first migration may fail because the tables already exist. Choose one schema owner before deployment. With the current config, CoreDB owns the table structure; update the service deployment/run instructions to skip `migrate deploy` for these databases (Prisma Client generation is still needed). If Prisma Migrate should remain the owner instead, remove the table schemas from CoreDB and let service migrations reconcile the tables.
