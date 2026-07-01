# Copilot Instructions For garmin

## Repo Summary

- Repo name: `garmin`
- Purpose: Pull, process, and visualize Garmin data.
- Role in larger system: Owns the Garmin domain logic, dashboard generation, and reusable code that feeds the public website.
- Website surface: Selected dashboard pages exposed at `/garmin` through `aws_flask_site`.
- Target `v2` integration mode: Generated dashboard artifacts or a small package boundary for selected live functionality.
- Sync boundary: The standalone repo is the development source of truth. Website integration still uses an embedded copy inside `aws_flask_site`, but that should be treated as transitional.

## Run And Validation

- Main local run command: `source .venv/bin/activate && python -m garmin.app.app`
- Main validation command: `source .venv/bin/activate && python -m compileall garmin`
- Optional quality commands when dev dependencies are installed: `source .venv/bin/activate && ruff check .`, `source .venv/bin/activate && mypy garmin`, and `source .venv/bin/activate && pytest`
- Deployment processes: `.github/workflows/deploy-ec2.yml` deploys the repo to EC2 on `prod`, and `.github/workflows/deploy-lambda.yml` deploys the Lambda updater on `prod`

## Branch Workflow

- Default stable branch: `main`
- Integration branch: `dev`
- Production branch: `prod`
- Long-lived overhaul branch if needed: use a repo-specific feature branch; do not assume this repo needs to move in lockstep with the first `v2` site milestone
- Release tagging rule: Tag release-ready commits on `main` as `vX.Y.Z`; `prod` should reflect deployable releases only.

## Important Paths

- Current repo-only handoff: `GARMIN_HANDOFF.md`
- App entrypoint: `garmin/app/app.py`
- Website-facing routes: `garmin/app/routes.py`
- Garmin session and scraper boundary: `garmin/api/`
- Core IO and S3 boundary: `garmin/io/file_manager.py`
- Current database boundary: `garmin/io/db_manager.py`
- Current updater flow: `garmin/updaters.py`
- Lambda deployment requirements: `garmin/requirements-lambda.txt`
- Packaging metadata: `pyproject.toml`, `setup.py`, `requirements.txt`
- Deployment workflows: `.github/workflows/deploy-ec2.yml`, `.github/workflows/deploy-lambda.yml`
- Data and generated artifacts: `data/`
- Generated or vendored files to avoid editing casually: `data/dashboards/`, `figures/`, cached processed data under `data/`

## Known Verified Versus Planned Items

- Verified: `garmin/api/` is the current Garmin Connect login and session boundary.
- Verified: runtime package code no longer imports `garth`, but the repo-owned login and refresh replacement is still in progress.
- Verified: `garmin/scripts/lambda_update.py` still instantiates `DatabaseManager` and writes through the RDS-backed path today.
- Verified: `garmin/app/routes.py` currently serves dashboard HTML artifacts through local cache plus S3 fetches.
- Verified: `pyproject.toml` and `setup.py` both define packaging metadata, and `pyproject.toml` currently claims a `src` layout even though the package lives under `garmin/` at the repo root.
- Verified: `README.md` is effectively empty and there is no visible `tests/` directory yet.
- Verified: live RDS inspection showed the populated Garmin-like historical tables are currently in the `public` schema, while the `garmin` schema is effectively empty.
- Planned: move Garmin analytical history off the shared RDS and toward private S3-backed analytical files.
- Planned: reduce the website's dependence on the embedded/submodule copy in `aws_flask_site`.

## Safety Rules

- Do not edit generated dashboard HTML or processed data unless the task is explicitly about regenerating them.
- Keep website-facing route changes compatible with `aws_flask_site` expectations unless coordinating both repos.
- Treat S3 bucket usage and `AWS_EXECUTION_ENV` logic as cross-repo integration boundaries.
- Do not spread Garmin provider-specific scraping logic beyond `garmin/api/`; keep any Garmin Connect replacement behind a narrow session/client interface.
- Do not make the packaging situation more fragmented; prefer one clear packaging source of truth.
- If you change anything that affects the website shell, note the required embedded-copy or integration update in `aws_flask_site`.
- The dashboard surface is currently stale and is not the first `v2` priority, so avoid broad Garmin redesign work unless it is explicitly in scope.
- Prefer AWS CLI or GitHub Actions over manual console work for Lambda or artifact operations.

## Architecture Notes

- Main framework or stack: Python 3.11, Flask, pandas, NumPy, Bokeh, boto3.
- Key integrations: Garmin data pulls, S3-backed dashboard artifact storage, `myutils`, EC2 deployment, Lambda updater, and the website embedded copy.
- Known fragile areas: unsupported upstream dependency risk in `garth`, local versus AWS file access, generated dashboard artifact flow, RDS versus file-backed data flow, and packaging differences between standalone and website-embedded usage.

## Data Storage Rules

- Treat private S3-backed analytical files as the target storage layer for Garmin data, not the shared website RDS.
- Keep raw or processed personal Garmin data private and expose only curated summaries or viewer-ready outputs to the website.
- Treat the populated Garmin-like tables currently in the live database `public` schema as legacy migration input, not as the desired long-term source of truth.
- If the Garmin Lambda only needs public API access, S3, and Secrets Manager, prefer a non-VPC Lambda so NAT is no longer required for this workload.
- If a relational store is still needed later, reserve it for small metadata or workflow state, not the main time-series history.

## Documentation Standards

- Public modules, classes, and functions should have docstrings.
- New public APIs should have type hints.
- Example usage should live in `README.md`, module docstrings, or focused docs added with the change.
- Keep the docs explicit about which outputs are exploratory versus website-facing artifacts.

## V2 Coordination Rules

- Treat this repo as the source of truth for Garmin logic even when the website still carries an embedded copy.
- Prefer publishing curated dashboard artifacts or a narrow reusable package surface over keeping the whole runtime embedded in the site forever.
- Prefer a site-owned adapter boundary in `aws_flask_site` over direct imports from Garmin internals.
- If a site change no longer needs a live Garmin runtime, prefer removing stale integration rather than preserving it by default.
- Preferred future viewer pattern: private S3 parquet or similar analytical files, queried or filtered through the backend, with DuckDB-like local exploration as a replacement for relying on RDS tables.

## Cleanup Priorities

- Priority 1: Stabilize or replace the Garmin Connect access path while keeping provider-specific logic isolated behind `garmin/api/` or a successor client module.
- Priority 2: Fix packaging and repo-shape inconsistencies so one build path, one dependency source of truth, and one package layout are clearly documented.
- Priority 3: Migrate analytical history away from shared RDS toward private S3-backed raw, curated, and viewer-ready outputs.
- Priority 4: Strengthen the package boundary between Garmin domain logic and website-facing artifacts.
- Priority 5: Add or modernize tests around IO, updater flows, and artifact generation.