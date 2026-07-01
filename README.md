# garmin

Pull, process, and visualize Garmin data.

## Current auth model

The repo no longer depends on `garth` for runtime auth. The active auth boundary is the repo-owned `garmin.api` package.

Recommended flow:

- Local development: use full Garmin username/password login once, then persist the repo-owned OAuth2 token locally.
- AWS Lambda: use Secrets Manager to store the repo-owned OAuth2 refresh token and refresh it on demand.
- Recovery path: if the stored token becomes invalid, re-bootstrap it locally with a full login and update the AWS secret.

Why this split:

- It avoids putting the Garmin username/password into every Lambda invocation path.
- It makes local development straightforward.
- It keeps the fragile login flow off the hot path in production.

## Environment variables

### Local development

- `GARMIN_USERNAME`: Garmin account email.
- `GARMIN_PASSWORD`: Garmin account password.
- `GARMIN_RUNTIME_ENV=local`: optional but recommended; forces local disk + SQLite behavior.

Local token storage:

- Primary repo-owned token cache: `data/sessions/garmin_connect/oauth2_token.json`
- Legacy fallback read path: `data/sessions/garth/`

### AWS / Lambda

- `GARMIN_USE_AWS_SECRETS=1`: force token storage to use Secrets Manager.
- `GARMIN_AWS_SECRET_NAME=garmin/oauth2_token`: OAuth2 token secret name.
- `AWS_REGION`: AWS region for Secrets Manager.
- `GARMIN_S3_BUCKET`: private Garmin bucket for curated parquet, processed outputs, and dashboard artifacts.

Legacy only:

- `DATABASE_URL`: only needed if you intentionally run the legacy database-backed path.

Notes:

- The OAuth1 secret is optional and is derived automatically by replacing `oauth2_token` with `oauth1_token`.
- Local runs no longer switch to AWS mode just because `AWS_EXECUTION_ENV` is set in `.env`; the code now requires real Lambda markers or an explicit override.

## Local setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

For test and lint tooling:

```bash
pip install -e .[dev]
```

Suggested local `.env` values:

```env
GARMIN_USERNAME=your-email@example.com
GARMIN_PASSWORD=your-password
GARMIN_RUNTIME_ENV=local
```

## Local commands

Run the app:

```bash
source .venv/bin/activate && python -m garmin.app.app
```

Run the updater locally against SQLite:

```bash
source .venv/bin/activate && python -m garmin.scripts.manual_update
```

The default updater entrypoint now writes curated parquet through the file-manager path. Locally that means `data/curated/...`; in Lambda that means S3 under the configured `GARMIN_S3_BUCKET`.

To test the AWS-backed file path locally before deploying Lambda:

```bash
source .venv/bin/activate && python -m garmin.scripts.manual_update --storage-target s3
source .venv/bin/activate && python -m garmin.scripts.manual_process_data --storage-target s3
```

Those commands keep the local Garmin auth flow, but they write curated, processed, and moving-average outputs through the S3-backed file manager instead of the repo-local `data/` directory.

Process curated daily parquet into processed outputs and moving averages:

```bash
source .venv/bin/activate && python -m garmin.scripts.manual_process_data
```

This processing step now reads curated daily parquet from the active file-manager path instead of querying the SQL tables directly.

Detailed backfill behavior:

- Detailed Garmin endpoints may only allow a limited amount of historical backfill per run.
- The updater now keeps pulling while Garmin continues serving data, then stops after the first cache-warm denial.
- Remaining detailed dates are left for the next run instead of continuing to hammer denied requests.

Validate imports/bytecode:

```bash
source .venv/bin/activate && python -m compileall src/garmin
```

## Bootstrapping Lambda tokens

1. Run a local login once so the repo-owned token file is created under `data/sessions/garmin_connect/`.
2. Publish the local token to Secrets Manager:

```bash
source .venv/bin/activate && python -m garmin.scripts.bootstrap_aws_auth
```

3. Set `GARMIN_USE_AWS_SECRETS=1` in Lambda.
4. Set `GARMIN_S3_BUCKET` in Lambda so curated parquet and downstream artifacts write to the Garmin bucket.
5. Keep full-login credentials out of Lambda unless you intentionally want a fallback recovery path.

## Bundle-style Lambda deployment

This repo now includes a SAM starter under `infra/sam/` so Lambda code, runtime env, IAM, and scheduling can be managed as a versioned deployment bundle rather than only through ad hoc CLI updates.

Local SAM build and deploy flow:

```bash
sam build -t infra/sam/template.yaml
sam deploy --guided --config-file infra/sam/samconfig.example.toml
```

Important migration note:

- The existing `garmin-data-updater` Lambda is currently an unmanaged function outside CloudFormation.
- The SAM template defaults to `garmin-data-updater-managed` to avoid colliding with that existing function during adoption.
- Once the managed function is validated, you can switch schedules or rename resources as part of the cutover.

The existing GitHub Actions workflow still supports direct code-and-config updates to the current `garmin-data-updater` function on `prod`.

## Current recommendation

Use full login for local bootstrap and manual recovery only. Use the repo-owned OAuth2 refresh token from Secrets Manager for Lambda and other unattended jobs.
