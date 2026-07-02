# Data Storage Notes

Use this file for Garmin storage and deployment decisions while working only in the `garmin` repo.

## Target Direction

- Garmin should move toward private S3-backed analytical storage, not the shared website RDS
- raw or processed personal Garmin data should stay private
- the website should consume only curated analytical outputs or dashboard-ready artifacts

## Current State

- Garmin Lambda currently exists and is actively invoked
- current observed failure mode is `HTTP 429 Too Many Requests` from Garmin backend auth exchange
- live RDS inspection showed the populated Garmin-like historical tables are in the `public` schema, while the `garmin` schema tables are effectively empty
- deployed updater path now writes curated Garmin outputs to `s3://my-garmin-data/curated/...`
- live Garmin Lambda no longer carries `DATABASE_URL`
- live Garmin Lambda now uses role-based S3 access and is no longer VPC-attached

## Storage Rules

- use private S3 for raw or processed Garmin datasets
- prefer parquet as the main analytical format
- use JSON only for small cache payloads or frontend-ready summaries
- use HTML artifacts only where the existing dashboard flow still needs them during transition
- avoid expanding RDS usage for Garmin time-series history
- if a relational store is needed later, reserve it for small metadata or workflow state rather than the main health/activity history

## Suggested S3 Layout

- `s3://<garmin-bucket>/raw/<dataset>/<date-partition>/...`
- `s3://<garmin-bucket>/curated/daily/<dataset>.parquet`
- `s3://<garmin-bucket>/curated/detailed/<dataset>/query_date=YYYY-MM-DD.parquet`
- `s3://<garmin-bucket>/curated/metadata/detailed_status/<dataset>.parquet`
- `s3://<garmin-bucket>/artifacts/dashboards/...`
- `s3://<garmin-bucket>/viewer-cache/...json`

## Lambda And Networking

- the deployed Garmin Lambda now follows the public API plus Secrets Manager plus S3 model without VPC attachment
- the former Garmin NAT dependency has been removed along with the VPC attachment
- do not reintroduce VPC + NAT complexity unless a future Garmin job truly needs a private VPC-only resource

## Viewer Pattern

- query or filter curated S3-backed datasets through the backend
- keep raw personal data out of the public website runtime path
- use auth-gated routes on the website for any viewer access

## Local Data Access

- use DuckDB over local parquet copies or synced S3 exports for table-style exploration
- DBeaver can be used against DuckDB if you want a familiar UI
- do not rely on the shared RDS as the long-term analytics browser for Garmin

## First Migration Tasks

1. Inventory and export the populated Garmin-like tables currently living in the `public` schema
2. Define the target S3 layout for raw, curated, and viewer-cache data
3. Repair or replace the Garmin connector path so ingestion is stable again
4. Keep the scraper/provider boundary narrow while that connector work happens
5. Continue shrinking or retiring the legacy DB-backed Garmin paths now that deployed ingestion already targets curated S3 outputs
6. Clean up or archive legacy prefixes and legacy tables intentionally after export decisions are made
