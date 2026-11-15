# Finsights Core DB

`finsights-coredb` provisions MySQL and PostgreSQL databases, users, grants, tables, indexes, and foreign keys from versioned YAML and JSON. It creates database structures; it does not copy application rows between MySQL nodes.

## MySQL placement

| Node | Server | Purpose | Managed databases |
|---|---|---|---|
| Node 1 | OCI MySQL Tokyo | AI-service-owned data only | `finsights_ai` (existing schema stays unchanged) |
| Node 2 | OCI MySQL Singapore | All backend APIs, collection, canonical financial data, assets, and operational history | `finsights_auth`, `finsights_user`, `finsights_market`, `finsights_market_data`, `finsights_news`, `finsights_fundamentals`, `finsights_disclosures`, `finsights_economy`, `finsights_assets`, `finsights_scraping` |

Every database used by backend services or backend-operated collectors belongs to Node 2. Node 1 is reserved for AI services; its existing `finsights_ai` schema is not changed in this design. Oracle Autonomous Database continues to hold RAG chunks and embeddings.

## Database boundaries

| Database | Node | Contents |
|---|---:|---|
| `finsights_ai` | 1 | Existing AI chat sessions and messages; user IDs are logical references. Unchanged. |
| `finsights_auth` | 2 | Backend authentication accounts and transactional outbox. |
| `finsights_user` | 2 | Backend profiles and watchlists; `account_id` is a logical reference to Auth. |
| `finsights_market` | 2 | MarketService API read model (`instruments`, quotes, candles, indexes, and the API-facing news projection). |
| `finsights_market_data` | 2 | Canonical exchanges, companies, securities, aliases, providers, quotes, prices, indexes, and market history. |
| `finsights_news` | 2 | Canonical publishers, articles, content, authors, media, rights, and security matches. |
| `finsights_fundamentals` | 2 | Statement periods/revisions, metric catalog, reported values, and source-label mappings. |
| `finsights_disclosures` | 2 | Issuer filings, corporate actions, and regulator notices. |
| `finsights_economy` | 2 | Macroeconomic sources, time series, and versioned observations. |
| `finsights_assets` | 2 | Object-storage buckets, objects, variants, rights, and checksums; bytes stay in OCI Object Storage. |
| `finsights_scraping` | 2 | Source registry, scrape runs/items, fetch attempts, checkpoints, retries, and collection audit history. |

Keep canonical data separate from the API read model while placing both on Node 2: `finsights_market_data` and `finsights_news` hold collector-owned records, while `finsights_market` keeps the existing MarketService contract. A publisher can materialize canonical market/news records into that read model on the same MySQL node. CoreDB creates structures and users; it does not copy rows between databases. Keep stable logical IDs such as `company_uid`, `security_uid`, `article_uid`, and `asset_uid` across databases. IDs crossing database boundaries remain application-level references so each app account can stay scoped to its own database.

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

Node 2 manages the full backend `finsights_` set and removes undeclared managed databases/users. Node 1 temporarily has `drop_missing_databases: false` and `drop_missing_users: false`: its target manifest contains only `finsights_ai`, but legacy backend databases/users are left untouched until data migration and cleanup are explicitly planned. This protects any rows still on Node 1; CoreDB schema reconciliation does not move table data. MySQL and HeatWave system schemas are outside the `finsights_` managed scope.

The MySQL administrator account `finsightsadmin` is not managed by CoreDB. Admin credentials and app passwords are kept in GitHub Actions secrets or local ignored `.env` files. MySQL app accounts require TLS. CA files are public trust anchors; each endpoint certificate uses `MySQL_Endpoint_Server` as its verified identity.

The workflow uses GitHub Environments `coredb-postgres`, `coredb-mysql-node-1`, and `coredb-mysql-node-2` to scope deployment settings.

## Service and collection account mapping

| Workload | Database | App account | Node |
|---|---|---|---:|
| AuthService | `finsights_auth` | `finsights_auth_app` | 2 |
| UserService | `finsights_user` | `finsights_user_app` | 2 |
| MarketService API read model | `finsights_market` | `finsights_market_app` | 2 |
| Asset metadata | `finsights_assets` | `finsights_assets_app` | 2 |
| News collection and canonical articles | `finsights_news` | `finsights_news_app` | 2 |
| Fundamentals collection | `finsights_fundamentals` | `finsights_fundamentals_app` | 2 |
| Disclosure collection | `finsights_disclosures` | `finsights_disclosures_app` | 2 |
| Economic data collection | `finsights_economy` | `finsights_economy_app` | 2 |
| Market data collection | `finsights_market_data` | `finsights_market_data_app` | 2 |
| Scraper run and fetch history | `finsights_scraping` | `finsights_scraping_app` | 2 |
| AI chat-history connection | `finsights_ai` | `finsights_ai_app` | 1 |

Backend collection workers write audit and canonical data to Node 2 using the Node 2 CA. AI chat history uses Node 1 and its CA. The workflow refreshes the three backend MySQL DSNs in the Kubernetes Secret `finsights-backend-secrets` after Node 2 is reconciled, while preserving JWT and RabbitMQ values.

## GitHub Actions secrets

Create these repository secrets with the exact names:

- PostgreSQL: `POSTGRES_ADMIN_PASSWORD`, `DAGSTER_DB_PASSWORD`.
- Node 1: `MYSQL_NODE_1_ADMIN_PASSWORD`, `MYSQL_AI_APP_PASSWORD`.
- Node 2: `MYSQL_NODE_2_ADMIN_PASSWORD`, `MYSQL_AUTH_APP_PASSWORD`, `MYSQL_USER_APP_PASSWORD`, `MYSQL_MARKET_APP_PASSWORD`, `MYSQL_ASSETS_APP_PASSWORD`, `MYSQL_NEWS_APP_PASSWORD`, `MYSQL_SCRAPING_APP_PASSWORD`.
- SSH: `FINSIGHTS_VPS_SSH_KEY`, `FINSIGHTS_VPS_KNOWN_HOSTS`.

The existing `MYSQL_SCRAPING_APP_PASSWORD` is reused for the Node 2 collection accounts for fundamentals, disclosures, economy, market data, and scraping until those domains receive dedicated runtime credentials. No password value is stored in this repository.

## Local use

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m coredb.cli apply --target mysql-node-1
```

Use `--target mysql-node-2` or `--target postgres` for the other targets. The local command reads `.env`; GitHub Actions transfers a short-lived secret file to the Finsights VPS and removes it after the run.
