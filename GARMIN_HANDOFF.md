# Garmin Repo Handoff

Use this file when working in the `garmin` repo by itself with no visibility into the other repos.

## What This Repo Owns

- Garmin Connect access and session management
- Garmin data pull and update logic
- Data shaping and processing for Garmin metrics
- Private storage outputs and dashboard artifacts derived from Garmin data
- The canonical Garmin domain logic that the website may later consume through artifacts or a narrow package boundary

## What This Repo Does Not Own

- The public website shell
- The long-term website route design
- Cross-repo deployment topology outside the Garmin-specific workflows

If a change affects how the website integrates Garmin outputs, call out the expected update needed in `aws_flask_site` rather than assuming both repos are open.

## Current Verified State

- `garmin/api/` is the current Garmin Connect session boundary.
- The repo no longer imports `garth` in runtime package code, but the replacement login and refresh flow is still incomplete.
- `garmin/scripts/lambda_update.py` still creates a `DatabaseManager` and writes through the database-backed update path.
- `garmin/io/db_manager.py` still points AWS execution at `DATABASE_URL` and local execution at `data/garmin.db`.
- `garmin/io/file_manager.py` already provides a local-versus-S3 file abstraction for dataframe and text artifacts.
- `garmin/app/routes.py` currently serves dashboard HTML artifacts by pulling them from S3 into local cache files.
- Live RDS inspection showed the populated Garmin-like historical tables are in the `public` schema, while the `garmin` schema itself is effectively empty.
- `pyproject.toml` and `setup.py` both define package metadata.
- `pyproject.toml` currently claims a `src` layout even though the actual package is at `garmin/` in the repo root.
- `README.md` is empty and there is no visible `tests/` directory yet.

## Main Problems To Keep In Mind

1. The current Garmin provider dependency is at risk because `garth` is unsupported.
2. The repo shape is inconsistent: packaging metadata is duplicated and the declared layout does not match the real one.
3. The data flow is split between RDS-backed tables and S3-backed artifacts.
4. The website still consumes Garmin through a transitional embedded/submodule integration in `aws_flask_site`.
5. The repo lacks enough repo-local documentation and test coverage for safe cleanup.

## Target Direction

The medium-term target is:

- Garmin access behind a narrow client boundary
- raw and curated Garmin analytical data stored privately outside the website RDS
- website-facing outputs published as artifacts or through a narrow, stable interface
- packaging and dependency management simplified so local runs, Lambda deploys, and website integration are easier to reason about

## Scraper And Session Direction

Treat scraper work as a boundary problem, not just a library swap.

What to preserve:

- one narrow Garmin session/client interface
- one place that knows how login, token refresh, and request execution work
- one place that knows how AWS Secrets Manager or local token files are used

What to avoid:

- spreading `garth` calls across pullers or updater modules
- tying token format too tightly to one third-party package
- mixing scraper replacement with large storage or website refactors in the same first change

Recommended approach:

1. Keep all Garmin-provider-specific logic isolated behind `garmin/api/` or a renamed successor module such as `garmin/clients/garmin_connect.py`.
2. Research a maintained replacement path for `garth` or a direct request-based approach.
3. Preserve a small interface such as `connect()`, `refresh_token()`, `get()`, and `post()` so pullers do not care which provider implementation is underneath.
4. Update token persistence logic only after the new access path is proven.

## Planned Data Storage Direction

Keep this repo pointed away from shared RDS for analytical history.

Desired storage layers:

- raw private pull outputs in S3
- curated analytical datasets in parquet
- viewer-ready summaries or dashboard fragments in JSON or HTML only where still needed

What should not be the long-term default:

- shared website RDS as the main home for Garmin time-series history
- relying on the live `public` schema tables as the intended future model

Current migration meaning:

- treat the populated Garmin-like tables in `public` as legacy export input
- treat the empty `garmin` schema as cleanup residue, not the canonical source
- move toward file-backed analytical storage that the website can consume without reintroducing RDS dependence

Suggested S3 shape:

- `s3://<garmin-bucket>/raw/<dataset>/<date-partition>/...`
- `s3://<garmin-bucket>/curated/<dataset>/year=YYYY/month=MM/...parquet`
- `s3://<garmin-bucket>/artifacts/dashboards/...`
- `s3://<garmin-bucket>/viewer-cache/...json`

## Planned Website Integration Direction

For `v2`, the website should not carry the whole Garmin runtime forever.

Preferred order:

1. artifact handoff for static or semi-static dashboards
2. narrow package boundary for any reusable Garmin domain logic the website truly needs
3. separate runtime or service only if the Garmin surface needs independent behavior that the website should not own

Practical implication:

- design this repo so it can publish outputs cleanly
- do not assume `aws_flask_site` should import deep Garmin internals long-term

## Repo Format And Packaging Direction

This repo needs packaging cleanup.

Current issues:

- `pyproject.toml` and `setup.py` both define package metadata
- `requirements.txt` also duplicates runtime dependencies
- `pyproject.toml` declares a `src` layout, but there is no `src/` package tree
- `garmin.egg-info/` exists, which suggests packaging artifacts are hanging around in the working tree

Target direction:

- use `pyproject.toml` as the primary packaging source of truth
- either adopt a real `src/` layout or remove the false `src`-layout declaration and keep the current top-level package layout intentionally
- reduce `setup.py` to a compatibility shim or remove it if no longer needed
- make `requirements.txt` clearly intentional: either generated from `pyproject.toml`, kept only for specific deploy surfaces, or minimized to a thin compatibility list

Do not change all of this blindly in one pass. First determine which deploy workflows still depend on `setup.py`, `requirements.txt`, or editable installs.

## Recommended Code Structure Direction

The current package is usable, but the responsibilities are mixed.

Current high-level layout:

- `garmin/api/`: provider/session boundary
- `garmin/pullers/`: Garmin fetch logic
- `garmin/updaters.py`: orchestration and write path
- `garmin/io/`: storage and persistence helpers
- `garmin/analysis/`: analysis and plotting logic
- `garmin/app/`: local Flask dashboard runtime
- `garmin/scripts/`: manual and Lambda entrypoints

Preferred direction over time:

- keep provider access isolated from domain logic
- keep storage abstractions separate from analysis logic
- keep dashboard artifact generation separate from website integration assumptions
- keep scripts thin and move real logic into importable modules

If you refactor structure, prefer incremental moves such as:

- `garmin/clients/` for provider-specific session code
- `garmin/storage/` or a cleaner `garmin/io/` split for raw, curated, and artifact paths
- `garmin/services/` or `garmin/pipelines/` for update orchestration
- `garmin/web/` only if the local Flask app remains important

## Coding Standards For This Repo

- Add docstrings to public modules, classes, and functions.
- Add type hints for new public APIs and non-trivial internal helpers.
- Keep scripts thin; put business logic in normal modules.
- Avoid mixing AWS environment detection, scraping logic, transformation logic, and persistence logic in one function.
- Prefer explicit configuration boundaries over hidden environment branching where practical.
- Keep generated dashboard outputs, cached files, and notebook artifacts out of core logic changes unless regeneration is part of the task.

## Documentation And Testing Direction

This repo needs better local onboarding and safer validation.

Recommended improvements:

1. Fill in `README.md` with setup, data-flow overview, run commands, and deployment notes.
2. Add a small `tests/` directory focused first on IO and updater behavior.
3. Add smoke tests or narrow checks for artifact generation.
4. Document which outputs are exploratory and which are website-facing artifacts.

Good first test targets:

- `garmin/io/file_manager.py`
- `garmin/io/db_manager.py` behavior that will remain during migration
- updater logic that decides what to fetch and what to upsert or emit

## Networking And AWS Direction

- If Garmin ingestion only needs public API access, Secrets Manager, and S3, it should not stay in a VPC just to preserve old RDS access.
- After Garmin no longer depends on RDS from Lambda, reassess VPC attachment and NAT need.
- Keep secrets in AWS Secrets Manager or local environment files outside git, not in repo files.

## Recommended Workstreams

Do not try to do everything at once. Treat these as parallel but separable workstreams.

### Workstream 1: Restore Garmin access

- investigate maintained alternatives to `garth`
- keep the provider seam narrow
- get pulls working again before large architectural migrations

### Workstream 2: Packaging and repo cleanup

- reconcile `pyproject.toml`, `setup.py`, and `requirements.txt`
- decide on real package layout
- document installation and development workflow clearly

### Workstream 3: Storage migration

- export legacy historical data from the live `public` schema if needed
- move new analytical outputs toward private S3-backed raw and curated files
- shrink dependence on database tables

### Workstream 4: Website boundary cleanup

- make website integration consume artifacts or stable interfaces
- reduce long-term dependence on embedded/submodule Garmin internals

### Workstream 5: Tests and docs

- fill in README
- add targeted tests
- document deploy and regeneration steps

## First Practical Steps For The Next Agent

1. Read `.github/copilot-instructions.md`, this file, and `DATA_STORAGE_NOTES.md`.
2. Verify how `garth` is currently failing and what token behavior must be preserved.
3. Inventory every place runtime dependencies are declared: `pyproject.toml`, `setup.py`, `requirements.txt`, and `garmin/requirements-lambda.txt`.
4. Determine whether deploy workflows truly need the current packaging duplication.
5. Propose the first safe change in one workstream only, rather than combining scraper replacement, packaging cleanup, and storage migration in the same first patch.