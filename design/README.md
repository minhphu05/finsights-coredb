# Database diagrams

Each `.dbml` file is one complete MySQL database diagram that can be pasted into [dbdiagram.io](https://dbdiagram.io). The JSON schemas under the MySQL node folders remain the reconciler's source of truth. Update a DBML diagram whenever its JSON schema changes.

## Placement

- **Node 1 · Tokyo:** `finsights_ai`, `finsights_assets`, `finsights_disclosures`, `finsights_economy`, `finsights_fundamentals`, `finsights_market_data`, `finsights_news`, and `finsights_scraping`.
- **Node 2 · Singapore:** `finsights_auth`, `finsights_market`, and `finsights_user`.

Node 1 owns AI chat history plus all canonical source, collection, and pipeline data. Node 2 owns the backend account/profile databases and the API-facing MarketService model. `finsights_ai` keeps its existing table design unchanged.

## Separate canonical data from backend reads

- `finsights_market_data` contains canonical market entities and provider observations written by data collectors.
- `finsights_market` contains the smaller backend read model consumed by MarketService. It has its own `instruments`, quote/candle, index, and article projection tables. This avoids duplicate table names in one database and keeps the service model independent from source storage.
- `finsights_news` contains canonical article records and ordered content. Stable article/security IDs link records logically to the backend projection.
- `finsights_scraping` stores source registry, run/item/fetch audit, and checkpoints; it does not hold article or market content.
- `finsights_assets` stores object metadata; bytes remain in OCI Object Storage.
- `finsights_fundamentals`, `finsights_disclosures`, and `finsights_economy` each own one data domain so revisions and source lineage remain manageable.
- `finsights_auth`, `finsights_user`, and `finsights_ai` stay isolated by service/domain. IDs crossing databases are logical references because MySQL cannot enforce cross-database/node foreign keys.

CoreDB creates schemas but does not replicate rows between nodes. A publishing/projection process must copy canonical market facts needed by the Node 2 read model. The current collector also stores canonical news on Node 1; any Node 2 API projection should be populated explicitly.

## Source-to-database mapping

| Collection area | Sources | Canonical destination |
|---|---|---|
| Market reference, quote, index, and price history | SSI API, DNSE OpenAPI, Vietstock DataFeed/public board | `finsights_market_data` |
| Financial statements and ratios | FiinGroup Datafeed and issuer reports | `finsights_fundamentals` |
| Issuer filings, corporate actions, and notices | HOSE, HNX, VSDC, UBCKNN, issuer investor-relations pages | `finsights_disclosures` |
| Financial news and associated companies/securities | CafeF, Tuổi Trẻ, Thanh Niên, VnExpress | `finsights_news` |
| Macro time series | GSO, NSDP, State Bank of Vietnam | `finsights_economy` |
| Source assets and downloaded-document metadata | OCI Object Storage plus database registry | `finsights_assets` |
| Crawl history, retry state, and provenance | All collection jobs | `finsights_scraping` |
| Backend account/profile state and MarketService reads | Finsights services | Node 2 databases |

The initial listed-stock collection target in the data plan is 15 symbols. AI extraction, RAG, and evaluation datasets are outside this backend/data schema change; the MySQL AI chat tables remain unchanged. The domain boundaries reflect public provider areas: [DNSE OpenAPI](https://developers.dnse.com.vn/docs/guide/intro/api_platform/) documents OHLC/index/market-depth products, while [FiinGroup Standard API](https://fiingroup.vn/en/standard-api.html) and its [corporate Datafeed](https://datafeed.fiingroup.vn/api-datafeed-en/api-corporate) cover company reference and financial information. DBML uses the official [DBML syntax](https://dbml.dbdiagram.io/docs/).

## Ingestion notes

Use source IDs, canonical URLs, and hashes to make retries idempotent. Financial statements and macro observations retain revisions rather than overwriting history. Store event timestamps in UTC, monetary values as decimals, and rights/source attribution with every collected record. Keep large documents, raw HTML, images, and intraday files in object storage; MySQL holds metadata, checksums, source URLs, and stable asset UIDs.
