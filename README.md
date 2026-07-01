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
- `DATABASE_URL`: database URL when running in AWS-backed mode.

Notes:

- The OAuth1 secret is optional and is derived automatically by replacing `oauth2_token` with `oauth1_token`.
- Local runs no longer switch to AWS mode just because `AWS_EXECUTION_ENV` is set in `.env`; the code now requires real Lambda markers or an explicit override.

## Local setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
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

Validate imports/bytecode:

```bash
source .venv/bin/activate && python -m compileall garmin
```

## Bootstrapping Lambda tokens

1. Run a local login once so the repo-owned token file is created under `data/sessions/garmin_connect/`.
2. Copy the contents of `oauth2_token.json` into the AWS secret named by `GARMIN_AWS_SECRET_NAME`.
3. Set `GARMIN_USE_AWS_SECRETS=1` in Lambda.
4. Keep full-login credentials out of Lambda unless you intentionally want a fallback recovery path.

## Current recommendation

Use full login for local bootstrap and manual recovery only. Use the repo-owned OAuth2 refresh token from Secrets Manager for Lambda and other unattended jobs.
