# Database diagrams

Each `.dbml` file is a complete MySQL database diagram for dbdiagram.io. The JSON schemas in `mysql-node-1/` and `mysql-node-2/` are the reconciler's source of truth. Keep the corresponding DBML diagram aligned with its JSON schema.

## Node placement

| Node | Database | Owner and purpose |
|---|---|---|
| 1 | `finsights_ai` | Existing AI chat sessions and messages. Its schema is unchanged. |
| 2 | `finsights_auth` | Authentication accounts and the transactional outbox. |
| 2 | `finsights_user` | User profiles and watchlists. `account_id` is a logical reference to Auth. |
| 2 | `finsights_market` | MarketService API read model: instruments, quotes, candles, indices, and news projections. |
| 2 | `finsights_market_data` | Canonical market reference data and provider observations, prices, quotes, and history. |
| 2 | `finsights_news` | Canonical publishers, articles, publication variants, content, authors, media, and security matches. |
| 2 | `finsights_fundamentals` | Financial statement periods/revisions, metric catalog, reported values, and source mappings. |
| 2 | `finsights_disclosures` | Issuer disclosures, corporate actions, and regulatory notices. |
| 2 | `finsights_economy` | Economic sources, series definitions, and revisioned observations. |
| 2 | `finsights_assets` | Object-storage bucket/object metadata, variants, rights, and checksums. Object bytes stay in OCI Object Storage. |
| 2 | `finsights_scraping` | Collection source registry, run/item/fetch history, retry outcomes, and checkpoints. |

Node 2 is the only MySQL node for backend APIs, collectors, and their operational history. `finsights_news` is on Node 2 because news collection and serving are backend data flows. Node 1 is reserved for AI-service-owned data; this design leaves the existing `finsights_ai` schema alone.

## Node 2 boundaries and data flow

- `finsights_market_data` owns canonical exchange, company, security, index, provider, and price/quote records. `finsights_market` preserves the existing API contract consumed by MarketService. `instruments.security_uid` maps serving instruments to canonical securities; a publisher materializes current facts while history stays in the canonical database.
- `finsights_news` owns canonical article identities and source-specific publications, rights, content, authors, media, categories, tags, and security matches. `finsights_market.news_articles` is the smaller API projection used by MarketService; its `article_uid` maps each projected row to the canonical article.
- `finsights_scraping` records collection attempts and outcomes. It is not a storage location for raw HTML, documents, images, or market files; those belong in object storage, with their metadata in `finsights_assets`.
- `finsights_fundamentals`, `finsights_disclosures`, and `finsights_economy` keep separate financial-data domains and preserve source labels, revisions, event dates, rights, and provenance.
- `finsights_auth` and `finsights_user` isolate authentication credentials from profile and watchlist data. The user service receives account IDs through the account-created event and does not own passwords.

Each database has a separate app account and remains independently migratable. Relationships inside a database use foreign keys. Cross-database identifiers (`account_id`, `company_uid`, `security_uid`, `article_uid`, and `asset_uid`) are logical references, not cross-database foreign keys; this keeps app grants scoped to one database. CoreDB creates schemas and users but does not copy rows or run the canonical-to-read-model publisher.

Key integrity choices in the Node 2 schemas:

- Daily market prices, index values, and valuations are idempotent per entity, provider, and trading date. Separate providers can retain different observations without overwriting each other; provider IDs are required for source provenance.
- An object can have several asset variants. `asset_variants` is keyed by (`original_asset_id`, `variant_code`) and keeps each generated variant linked to its object metadata.
- The MarketService index read model requires a unique `symbol` and `name`, matching the fields queried by the current service implementation.

## Source-to-database mapping

| Collection area | Example sources | Canonical destination on Node 2 |
|---|---|---|
| Market reference, quotes, indexes, and history | SSI API, DNSE OpenAPI, Vietstock DataFeed/public board | `finsights_market_data` |
| Financial statements and ratios | FiinGroup Datafeed and issuer reports | `finsights_fundamentals` |
| Issuer filings, corporate actions, and notices | HOSE, HNX, VSDC, UBCKNN, issuer investor-relations pages | `finsights_disclosures` |
| Financial news and associated companies/securities | CafeF, Tuổi Trẻ, Thanh Niên, VnExpress | `finsights_news` |
| Macro time series | GSO, NSDP, State Bank of Vietnam | `finsights_economy` |
| Source files and downloaded-document metadata | OCI Object Storage plus database registry | `finsights_assets` |
| Crawl history, retries, and provenance | All collection jobs | `finsights_scraping` |
| Backend API serving model | Finsights services and data publishers | `finsights_market` |

Use provider/source IDs and URL hashes for idempotency. Retain financial and macro revisions rather than overwriting source history. Store timestamps in UTC and monetary values as decimals. Default news/document rights to link-only unless the source permits retaining content. Large documents, raw HTML, images, and intraday files stay in object storage.

## Safe placement cutover

The Node 1 target manifest now contains only `finsights_ai`, but Node 1's `drop_missing_databases` and `drop_missing_users` are disabled during the placement cutover. This prevents a schema push from deleting existing Node 1 rows or accounts. Node 2 is configured to create all backend databases. Reconciliation provisions structure only; it does not move data from Node 1 to Node 2. Keep legacy Node 1 data until a separate copy/verification and cleanup plan is approved.

## References

The source boundaries follow the provider areas described by [DNSE OpenAPI](https://developers.dnse.com.vn/docs/guide/intro/api_platform/), [FiinGroup Standard API](https://fiingroup.vn/en/standard-api.html), and [FiinGroup corporate Datafeed](https://datafeed.fiingroup.vn/api-datafeed-en/api-corporate). Diagrams use the [DBML syntax](https://dbml.dbdiagram.io/docs/).
