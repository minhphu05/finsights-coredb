# Finsights Core DB

`finsights-coredb` provisions MySQL and PostgreSQL databases, users, grants, tables, indexes, and foreign keys from versioned YAML and JSON. It creates database structures; it does not copy application rows between MySQL nodes.

## MySQL placement

| Node | Server | Purpose | Managed databases |
|---|---|---|---|
| Node 1 | OCI MySQL Tokyo | AI chat history and canonical collection data | `finsights_ai`, `finsights_assets`, `finsights_disclosures`, `finsights_economy`, `finsights_fundamentals`, `finsights_market_data`, `finsights_news`, `finsights_scraping` |
| Node 2 | OCI MySQL Singapore | Backend-owned service databases and API read model | `finsights_auth`, `finsights_market`, `finsights_user` |

The database split keeps each data domain separate. Node 1 owns source data and collection history; Node 2 owns backend account/profile state and the market API read model. `finsights_ai` remains unchanged on Node 1. Oracle Autonomous Database continues to hold RAG chunks and embeddings.

## Database boundaries

| Database | Node | Contents |
|---|---:|---|
| `finsights_ai` | 1 | AI chat sessions and messages; user IDs are logical references. |
| `finsights_assets` | 1 | Object-storage bucket, object, variant, rights, and checksum metadata; bytes stay in OCI Object Storage. |
| `finsights_disclosures` | 1 | Issuer filings, corporate actions, and regulator notices. |
| `finsights_economy` | 1 | Macroeconomic time series and versioned observations. |
| `finsights_fundamentals` | 1 | Statement periods/revisions, financial metrics, reported values, and source-label mappings. |
| `finsights_market_data` | 1 | Canonical exchanges, securities, aliases, providers, prices, quotes, indexes, and market history from collectors. |
| `finsights_news` | 1 | Canonical publisher, article, content, author, media, and security-association data. |
| `finsights_scraping` | 1 | Source registry, scrape runs/items, fetch attempts, and checkpoints. This is audit/retry history, not a content store. |
| `finsights_auth` | 2 | Backend authentication accounts and transactional outbox. |
| `finsights_market` | 2 | MarketService API read model (`instruments`, quotes, candles, indexes, and the API-facing news projection). |
| `finsights_user` | 2 | Backend profiles and watchlists. |

`finsights_market_data` and `finsights_market` are intentionally separate. The former stores canonical collection data; the latter is the backend-facing model consumed by MarketService. `finsights_news` is canonical article storage. MarketService's smaller news projection belongs in Node 2. CoreDB does not replicate data or create cross-node foreign keys. A projection/transfer process must publish the needed canonical market rows from Node 1 into Node 2; news writes currently need the same separation. Keep stable logical IDs such as `company_uid`, `security_uid`, `article_uid`, and `asset_uid` across databases.

## Schemas and DBML

- YAML manifests under `mysql-node-1/` and `mysql-node-2/` define database placement and app accounts.
- Each database has one JSON schema under its node's `schemas/` directory.
- `design/` contains one DBML diagram per database for dbdiagram.io. Keep the matching diagram in sync when its JSON schema changes.
- `postgres/config.yaml` provisions the Dagster PostgreSQL database separately; Dagster owns its internal tables.

PostgreSQL is reserved for platform tools. CoreDB provisions `dagster` with the `dagster_admin` login and hands ownership of existing public tables/sequences to that role. Dagster creates and migrates its own internal tables.

## Data conventions

- Store monetary and ratio values as `DECIMAL`, never floating-point columns.
- Store event timestamps in UTC; business/reporting periods use date boundaries.
- Keep provider/source IDs and original labels beside normalized values.
- Preserve statement and macro revisions rather than overwriting source history.
- Keep large documents, raw HTML, images, and market-data files in object storage. MySQL stores source URL, checksum, MIME type, rights status, and asset UID.
- Default document/news rights to link-only unless collection rights allow retaining content.
- Make ingestion idempotent with source IDs, URL hashes, period keys, or source/time keys.

## Reconciliation and reset

GitHub Actions runs `.github/workflows/reconcile.yml`. Changes under one MySQL node folder reconcile that node; `workflow_dispatch` runs all targets. The MySQL jobs run over the Finsights VPS because the GitHub runner cannot reach the MySQL allowlist directly.

Both manifests set `drop_missing_databases` and `drop_missing_users` to `true`. CoreDB therefore removes `finsights_` databases/users that are not declared on that node, then creates the desired databases and schemas. MySQL and HeatWave system schemas are outside the `finsights_` managed scope.

The MySQL administrator account `finsightsadmin` is not managed by CoreDB. Admin credentials and app passwords are kept in GitHub Actions secrets or local ignored `.env` files. MySQL app accounts require TLS. CA files are public trust anchors; each endpoint certificate uses `MySQL_Endpoint_Server` as its verified identity.

The workflow uses GitHub Environments `coredb-postgres`, `coredb-mysql-node-1`, and `coredb-mysql-node-2` to scope deployment settings.

## Backend connection mapping

| Backend setting | Database | App account | Node |
|---|---|---|---:|
| `AUTH_MYSQL_DSN` | `finsights_auth` | `finsights_auth_app` | 2 |
| `USER_MYSQL_DSN` | `finsights_user` | `finsights_user_app` | 2 |
| `MARKET_MYSQL_DSN` | `finsights_market` | `finsights_market_app` | 2 |
| AI chat-history connection | `finsights_ai` | `finsights_ai_app` | 1 |

Collection workers write audit and canonical source data to Node 1 using the Node 1 CA. The workflow refreshes the three backend MySQL DSNs in the Kubernetes Secret `finsights-backend-secrets` after Node 2 is reconciled, while preserving JWT and RabbitMQ values.

## GitHub Actions secrets

Create these repository secrets with the exact names:

- PostgreSQL: `POSTGRES_ADMIN_PASSWORD`, `DAGSTER_DB_PASSWORD`.
- Node 1: `MYSQL_NODE_1_ADMIN_PASSWORD`, `MYSQL_AI_APP_PASSWORD`, `MYSQL_ASSETS_APP_PASSWORD`, `MYSQL_NEWS_APP_PASSWORD`, `MYSQL_SCRAPING_APP_PASSWORD`.
- Node 2: `MYSQL_NODE_2_ADMIN_PASSWORD`, `MYSQL_AUTH_APP_PASSWORD`, `MYSQL_MARKET_APP_PASSWORD`, `MYSQL_USER_APP_PASSWORD`.
- SSH: `FINSIGHTS_VPS_SSH_KEY`, `FINSIGHTS_VPS_KNOWN_HOSTS`.

The existing `MYSQL_SCRAPING_APP_PASSWORD` is reused for the planned Node 1 collector accounts for fundamentals, disclosures, economy, and market data until those domains receive dedicated runtime credentials. No password value is stored in this repository.

## Local use

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m coredb.cli apply --target mysql-node-1
```

Use `--target mysql-node-2` or `--target postgres` for the other targets. The local command reads `.env`; GitHub Actions transfers a short-lived secret file to the Finsights VPS and removes it after the run.
