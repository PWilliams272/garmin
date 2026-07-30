# Garmin AI Agent Handoff

This file is a current passoff snapshot for another AI agent working in this repo.

## Executive Summary

- The repo's main functional path is now curated-storage-first, not database-first.
- The active AWS Lambda is `garmin-data-updater` in `us-east-2`.
- Lambda auth uses a repo-owned Garmin token stored in AWS Secrets Manager, not full username/password on each run.
- Lambda writes curated parquet outputs to S3 bucket `my-garmin-data`.
- The major July 2026 operational issue was not a Garmin auth loop. It was a Lambda resource problem: the function was configured with `128 MB` and timed out at `900s` during normal updater work.
- Increasing the live Lambda memory to `512 MB` fixed the timeout immediately.
- Additional logging was added and deployed so detailed-dataset timing is now visible in CloudWatch logs.
- A small state bug was also fixed: detailed cache-warm denial state is now reset per dataset pull instead of leaking across datasets inside one `update_all()` run.

## Where We Left Off

### Completed recently

- Investigated the last three failed Lambda runs.
- Confirmed the old failures were `Status: timeout`, not a crash, and not primarily a 429 auth failure.
- Confirmed the function at `128 MB` was pegging memory near the cap and taking too long.
- Raised the live Lambda memory setting from `128 MB` to `512 MB`.
- Manually invoked the Lambda and confirmed it completed successfully at `512 MB`.
- Added detailed timing logs to the curated detailed updater path.
- Deployed the new code to the live Lambda.
- Verified the new log lines are live in CloudWatch.

### Current operational state

- The Lambda is currently healthy at `512 MB`.
- The deployed function completed a manual validation run in about `9.7s` after the code and config updates.
- Detailed pull logging now emits queue size, pull duration, write duration, and status-merge duration for each detailed dataset.

### Current unresolved areas

- Garmin provider/auth remains fragile overall and still needs continued hardening or replacement work.
- Legacy DB-backed code paths and legacy S3 prefixes still exist and should be retired intentionally.
- Packaging and deploy conventions are still somewhat duplicated and inconsistent.
- The repo still has a transitional website boundary with `aws_flask_site`.

## Important Recent Findings

### Lambda timeout diagnosis

The original suspicion was that the updater detected cache-warm denial correctly but kept running too long afterward. After deeper inspection, the more important finding was simpler:

- At `128 MB`, Lambda was just too underprovisioned for this workload.
- At `512 MB`, the same updater completed normally.

Evidence gathered:

- Old CloudWatch `REPORT` lines showed `Duration: 900000 ms`, `Status: timeout`, `Memory Size: 128 MB`, and `Max Memory Used` at or near the limit.
- A local targeted run of `HeartRateDetailed` completed in about `1m40s`, which suggested logic was not fundamentally stuck.
- After increasing Lambda memory to `512 MB`, a full live invocation completed in about `94.9s`.
- After deploying the new code with logging, a validation invoke completed in about `9.7s` with detailed timing lines visible.

### Detailed puller state bug

The detailed puller keeps `_cache_warm_denied` on the `HealthDetailedPuller` instance.

Why that mattered:

- `DataUpdater.update_all()` reuses one `HealthDetailedPuller` instance across multiple detailed datasets.
- Before the fix, if one dataset hit cache-warm denial, the next dataset in the same invocation could inherit that state.
- That could cause a later dataset to skip its own first cache-warm attempt and behave as if denial had already happened.

Fix applied:

- Reset `_cache_warm_denied` and `_last_pull_status` at the start of each `pull_data(...)` call in `src/garmin/pullers/health_detailed.py`.

### New logging added

The curated detailed updater in `src/garmin/updaters.py` now logs:

- start of each detailed dataset update with queued date count
- total time spent in the detailed pull itself
- total time spent writing per-day detailed parquet files
- total time spent merging detailed status metadata

This now makes it easy to distinguish:

- Garmin-side pull delays
- parquet/S3 write delays
- status metadata merge delays

## Files Changed During This Investigation

- `src/garmin/pullers/health_detailed.py`
- `src/garmin/updaters.py`

### Behavior changed in `src/garmin/pullers/health_detailed.py`

- reset per-dataset denial state at the start of `pull_data(...)`
- reset `_last_pull_status` at the same point

### Behavior changed in `src/garmin/updaters.py`

- added timing logs around detailed curated update phases

## Live AWS State

### Account and region

- AWS account: `545009868532`
- region: `us-east-2`
- preferred operator profile: `admin`

### Main Garmin resources

- Lambda: `garmin-data-updater`
- S3 data bucket: `my-garmin-data`
- S3 config bucket: `my-garmin-config`
- Secrets Manager token secret: `garmin/oauth2_token`

### Current Lambda configuration

Verified during this work:

- runtime: `python3.11`
- handler: `garmin.scripts.lambda_update.lambda_handler`
- timeout: `900`
- memory: `512 MB`
- no VPC attachment
- environment variables:
  - `GARMIN_USE_AWS_SECRETS=1`
  - `GARMIN_AWS_SECRET_NAME=garmin/oauth2_token`
  - `GARMIN_S3_BUCKET=my-garmin-data`

### Current Lambda logging location

- CloudWatch log group: `/aws/lambda/garmin-data-updater`

## Auth Model

### Local

- Local bootstrap uses Garmin username/password once.
- Local token path: `data/sessions/garmin_connect/oauth2_token.json`
- Legacy fallback path may still exist under `data/sessions/garth/`, but current runtime auth is repo-owned and not `garth`-based.

### AWS

- Lambda should use the OAuth2 refresh token from Secrets Manager.
- Lambda should not depend on full Garmin username/password on every invocation.
- Recovery path if token becomes invalid:
  - perform local login/bootstrap again
  - refresh the secret in AWS

### Important auth boundary

- Keep provider-specific Garmin access logic isolated behind `garmin/api/`.
- Do not spread provider/session assumptions through pullers or updater code.

## Storage Model

### Intended storage direction

- private S3-backed analytical storage, not shared website RDS
- parquet for main analytical outputs
- JSON only for smaller cache or viewer payloads
- HTML artifacts only where still needed for existing dashboard flow

### Current effective storage layers

- curated daily parquet: `s3://my-garmin-data/curated/daily/`
- curated detailed per-day parquet: `s3://my-garmin-data/curated/detailed/<dataset>/query_date=YYYY-MM-DD.parquet`
- curated detailed status metadata: `s3://my-garmin-data/curated/metadata/detailed_status/<dataset>.parquet`
- legacy prefixes still present: `processed/`, `moving_averages/`, `dashboards/`

### Database status

- Live RDS inspection previously showed the meaningful historical Garmin-like tables were in the `public` schema.
- The `garmin` schema itself was effectively empty.
- Treat RDS as legacy migration input, not the target long-term analytical store.

## Package and Repo Shape

### Important paths

- main handoff: `GARMIN_HANDOFF.md`
- storage notes: `DATA_STORAGE_NOTES.md`
- app entrypoint: `src/garmin/app/app.py`
- Flask routes: `src/garmin/app/routes.py`
- Garmin auth/session boundary: `src/garmin/api/`
- file abstraction: `src/garmin/io/file_manager.py`
- curated storage: `src/garmin/io/curated_store.py`
- legacy DB boundary: `src/garmin/io/db_manager.py`
- updater orchestration: `src/garmin/updaters.py`
- detailed puller: `src/garmin/pullers/health_detailed.py`
- Lambda entrypoint: `src/garmin/scripts/lambda_update.py`
- manual update entrypoint: `src/garmin/scripts/manual_update.py`
- processing entrypoint: `src/garmin/scripts/manual_process_data.py`
- Lambda deploy workflow: `.github/workflows/deploy-lambda.yml`
- SAM starter: `infra/sam/template.yaml`

### Packaging cautions

- `pyproject.toml` is the intended packaging source of truth.
- `setup.py` still exists as a compatibility shim.
- `requirements.txt` and `src/garmin/requirements-lambda.txt` still duplicate some dependency declarations.
- Do not assume every workflow is fully aligned yet.

### Deploy-path caution

- The checked-in GitHub Actions Lambda workflow copies `src/garmin/` into a build directory.
- During this work, local manual deployment also used `src/garmin/` as the source package path.
- Be careful not to accidentally package from stale `lambda_build/` contents.

## How Lambda Was Deployed During This Work

The live code deployment in this session was done manually from the local machine using the same broad packaging shape as the GitHub Actions workflow.

Summary of what was done:

- copy `src/garmin/` into a temporary build directory
- copy `src/garmin/requirements-lambda.txt` into that temp package as `requirements.txt`
- `pip install -r requirements.txt -t <temp package dir>`
- zip the temp package dir
- run `aws lambda update-function-code --function-name garmin-data-updater --zip-file fileb://... --profile admin --region us-east-2`
- wait for update completion

This deployment completed successfully on `2026-07-06`.

## Useful CLI Commands

### Inspect Lambda config

```bash
aws lambda get-function-configuration \
  --function-name garmin-data-updater \
  --profile admin \
  --region us-east-2
```

### Invoke Lambda manually

```bash
aws lambda invoke \
  --function-name garmin-data-updater \
  --profile admin \
  --region us-east-2 \
  /tmp/garmin-lambda-response.json
```

### List recent Lambda log streams

```bash
aws logs describe-log-streams \
  --log-group-name /aws/lambda/garmin-data-updater \
  --order-by LastEventTime \
  --descending \
  --max-items 10 \
  --profile admin \
  --region us-east-2
```

### Pull the timing logs added in July 2026

```bash
aws logs get-log-events \
  --log-group-name /aws/lambda/garmin-data-updater \
  --log-stream-name 'STREAM_NAME_HERE' \
  --start-from-head \
  --profile admin \
  --region us-east-2 \
  --query 'events[?contains(message, `Starting curated detailed update`) || contains(message, `Detailed pull for`) || contains(message, `Wrote`) || contains(message, `Merged`) || contains(message, `Saved curated detailed data`) || contains(message, `REPORT`) || contains(message, `Exception`) || contains(message, `ERROR`)].message'
```

### Check top-level S3 layout

```bash
aws s3 ls s3://my-garmin-data --profile admin --region us-east-2
```

### Count detailed day files for a dataset

```bash
aws s3 ls s3://my-garmin-data/curated/detailed/heart_rate_detailed/ --recursive --profile admin --region us-east-2 | wc -l
```

## Local Validation Commands

### Compile touched modules

```bash
source .venv/bin/activate && python -m compileall src/garmin/pullers/health_detailed.py src/garmin/updaters.py
```

### Run one detailed dataset locally against AWS-backed storage

```bash
source .venv/bin/activate && time GARMIN_RUNTIME_ENV=aws GARMIN_USE_AWS_SECRETS=1 GARMIN_AWS_SECRET_NAME=garmin/oauth2_token GARMIN_S3_BUCKET=my-garmin-data python - <<'PY'
from garmin.api import GarminSession
from garmin.io.file_manager import FileManager
from garmin.io.curated_store import CuratedDataStore
from garmin.updaters import DataUpdater
from garmin.io.models import HeartRateDetailed

session = GarminSession()
file_manager = FileManager(environment="aws")
curated_store = CuratedDataStore(file_manager=file_manager)
updater = DataUpdater(session=session, curated_store=curated_store)
updater.update(HeartRateDetailed)
PY
```

## What Another Agent Should Check First

1. Read `GARMIN_HANDOFF.md`, `DATA_STORAGE_NOTES.md`, `README.md`, and this file.
2. Confirm the live Lambda is still at `512 MB` and not reverted.
3. Inspect the most recent CloudWatch stream to confirm the detailed timing logs are still appearing.
4. If a new failure happens, distinguish first between:
   - Garmin auth/provider failure
   - Lambda timeout/resource regression
   - S3/parquet write slowdown
5. Preserve the `garmin/api/` auth boundary while making any provider fixes.

## Recommended Next Tasks

### High priority

- Monitor scheduled Lambda runs to confirm the timeout regression is fully resolved in real scheduled traffic, not just manual invokes.
- Keep the Lambda at `512 MB` unless new evidence shows a different target is better.
- Continue watching for real Garmin auth/provider failures separate from infrastructure timing issues.

### Medium priority

- Add or update tests around detailed updater control flow and status handling.
- Review whether the new logging should be kept as-is or slightly condensed once confidence is high.
- Tighten documentation around the exact production Lambda deploy flow.

### Longer-term

- Continue packaging cleanup across `pyproject.toml`, `setup.py`, `requirements.txt`, and `requirements-lambda.txt`.
- Continue migration away from legacy DB-backed behavior and legacy S3 prefixes.
- Improve the separation between repo-owned Garmin logic and website integration.
- Reassess the Garmin provider implementation and replacement strategy if upstream behavior worsens.

## Things To Avoid

- Do not reintroduce VPC/NAT complexity for this Lambda without a concrete new dependency that requires it.
- Do not put Garmin username/password into recurring Lambda runtime config.
- Do not assume RDS is the target long-term storage model.
- Do not package or deploy from stale generated folders by accident.
- Do not spread Garmin-provider-specific behavior outside `garmin/api/` unless the boundary is being intentionally refactored.
