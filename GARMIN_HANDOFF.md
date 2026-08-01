# Garmin Repo — AWS/Ops Reference

Deeper reference for AWS state and CLI operations. Start with `CLAUDE.md` for repo architecture/rules; this file is for when you actually need to touch AWS.

## What this repo owns vs. doesn't

Owns: Garmin Connect access/session management, data pull/update/processing logic, private storage outputs and dashboard artifacts.

Doesn't own: the public website shell, cross-repo deployment topology. If a change affects how `aws_flask_site` integrates Garmin outputs, call that out rather than assuming both repos are open.

## Verified live state

- Lambda: `garmin-data-updater` (pull only), region `us-east-2`, runtime `python3.11`, handler `garmin.scripts.lambda_update.lambda_handler`, timeout `900s`, memory `512 MB`, no VPC attachment, role-based S3 access (no bucket-policy workaround), zip-packaged, deployed via `.github/workflows/deploy-lambda.yml` on push to `prod`.
  - Was stuck on 2026-07-06-era code for weeks (nobody had pushed `prod` since) despite dozens of commits landing on `main`/feature branches -- redeployed 2026-08-01 alongside the FIT-detail-pulling registry wiring below. If this pipeline seems to be missing recent work again, check `git log --oneline origin/prod..HEAD` and the Lambda's actual `LastModified` before assuming an auth/data problem.
  - Env: `GARMIN_USE_AWS_SECRETS=1`, `GARMIN_AWS_SECRET_NAME=garmin/oauth2_token`, `GARMIN_S3_BUCKET=my-garmin-data`.
  - CloudWatch log group: `/aws/lambda/garmin-data-updater`.
  - Was previously misconfigured at 128MB and timing out; fixed 2026-07 by raising to 512MB. If timeouts recur, check memory/duration in the `REPORT` log line before assuming an auth regression.
  - **Has an attached Lambda layer** (`AWSSDKPandas-Python311`, AWS-managed, ARN `arn:aws:lambda:us-east-2:336392948345:layer:AWSSDKPandas-Python311:1`) supplying pandas/numpy/pyarrow — not visible anywhere in this repo's config (no SAM/IaC manages it), only discoverable via `aws lambda get-function --function-name garmin-data-updater`. This is why `src/garmin/requirements-lambda.txt` doesn't list pandas/numpy despite `updaters.py` needing them. If this Lambda is ever recreated from scratch, that layer has to be reattached manually or pulls will fail on import.
  - EventBridge schedule: rule `run-every-day`, `cron(0 3 * * ? *)` (3:00 UTC daily).
- Lambda: `garmin-data-analyzer` (quality classification + GP/STS trend fitting + `curated/viewer_cache/` build — see `src/garmin/scripts/lambda_analyze.py`), same account/region, runtime `python3.11`, memory `1024 MB`, timeout `900s`, **container image**, not zip — `scipy`+`scikit-learn`+`statsmodels` alone are ~200MB+ uncompressed, over a standard Lambda's 250MB unzipped code+layers limit. Deployed via `.github/workflows/deploy-lambda-analyzer.yml` (ECR push + `update-function-code --image-uri`) on push to `prod`.
  - Image built from `infra/docker/analyzer.Dockerfile` — uses a plain `python:3.11-slim` base + `awslambdaric` (AWS's documented "alternative base image" pattern), not `public.ecr.aws/lambda/python`, because that AWS-provided base image is Amazon Linux 2 (glibc 2.26), too old for the manylinux_2_28+ wheels current numpy/scipy ship — pip would fall back to a from-source build with no compiler present. Debian's current glibc takes the prebuilt wheels directly.
  - ECR repo: `garmin-data-analyzer` (`545009868532.dkr.ecr.us-east-2.amazonaws.com/garmin-data-analyzer`).
  - IAM role: `garmin-data-analyzer-role` — S3 read/write on `my-garmin-data` only (same policy shape as the updater's `GarminS3DataAccess`), no Secrets Manager, no VPC (this Lambda never talks to Garmin or needs auth).
  - The multiscale GP trend fit (`fit_gp_multiscale_trend`) is currently disabled (`analysis_pipeline.FIT_GP_TREND = False`) — it's not rendered anywhere in the app (`quick_dashboard.html`'s `SHOW_GP_TREND = false`), and fitting it against ~4000-day daily series was what pushed memory past 1024MB and got the function OOM-killed. With it off, observed peak is ~340-430MB. Re-enable only alongside raising memory back up (tested working at 2048MB) if the GP comparison view comes back.
  - EventBridge schedule: rule `garmin-analyzer-daily`, `cron(30 3 * * ? *)` (3:30 UTC daily, 30 min after the puller).
- S3 buckets: `my-garmin-data` (curated parquet, analyzed layer, viewer-cache JSON, artifacts), `my-garmin-config`.
- Secrets Manager: `garmin/oauth2_token` (OAuth2 refresh token; OAuth1 secret is derived by name, `oauth1_token`).
- Curated S3 layout: `curated/daily/<dataset>.parquet`, `curated/detailed/<dataset>/query_date=YYYY-MM-DD.parquet`, `curated/metadata/detailed_status/<dataset>.parquet`, `curated/analyzed/<dataset>/...`, `curated/activities/summary|detail/...`, `viewer_cache/<page>_<source>.json`. Legacy prefixes `processed/`, `moving_averages/`, `dashboards/` still exist and should be retired intentionally, not by accident.
- As of 2026-08-01, `curated/activities/` (all sports, not just running/strength) is backfilled to S3 -- summaries for 12 activity types plus FIT-based per-point detail (`<sport>_timeseries` datasets) for every activity that has one. `_activity_type_registry()` now wires `get_activity_detail_timeseries` as `detail_fn` for every cardio sport (previously only strength had a `detail_fn`; FIT pulling existed but was never in the scheduled registry), so new activities get their per-point detail pulled automatically on the daily `garmin-data-updater` run. `garmin.scripts.manual_backfill_activity_details` (resumable, skips ids that already have a detail file) is what filled in the pre-existing summary-only history; re-run it (`--storage-target s3`) if a future gap ever needs backfilling again.
- Standalone viewer app: `garmin.peterwilliams.dev`, live as of 2026-08-01 -- same shared EC2 host as `kaya`/`aws_monitor` (`3.16.167.114`), code at `/home/ubuntu/garmin_viewer`, `garmin-viewer.service` (gunicorn, port `8030`, `127.0.0.1` only). Reuses the existing Flask app as-is (not a FastAPI rewrite, despite `WEB_APP_SETUP.md` describing FastAPI -- a deliberate deviation to avoid re-templating already-built, tested Jinja pages; gunicorn instead of uvicorn as the only infra consequence).
  - **Auth (updated 2026-08-01)**: nginx vhost `/etc/nginx/sites-available/garmin-viewer` uses `auth_request` against `aws_flask_site`'s `https://peterwilliams.dev/internal/auth-check`, validated via a short-lived (1hr) itsdangerous-signed token -- *not* a shared-cookie-domain scheme (that approach broke main-site login when tried for Kaya, commit `525aad1`, deliberately reverted). `aws_flask_site`'s `/garmin` route mints the token and appends it as `?token=...` on the iframe `src`; nginx reads it from `$arg_token` on the first request and bridges it into an httponly `garmin_auth` cookie (`Set-Cookie` via `add_header`, conditioned on `$arg_token` being present) so subsequent same-origin requests from the SPA (no query string) still validate via `$cookie_garmin_auth`. The auth-check subrequest forwards whichever is present as the `X-Iframe-Token` header. Replaced an initial HTTP Basic Auth-gated version (config backed up at `/etc/nginx/sites-available/garmin-viewer.bak-basicauth` on the host) that was live for a few hours on 2026-08-01 before `aws_flask_site` shipped the token endpoint -- if this auth stops working, check the token endpoint/contract before assuming nginx regressed, since the two repos' deploys aren't coordinated by any single CI.
  - Deployed via `.github/workflows/deploy-viewer-app.yml` (rsync + `pip install -e .` + `systemctl restart`) on push to `prod`, mirroring `kaya`'s/`aws_monitor`'s own viewer deploy workflows. Secrets: `GARMIN_VIEWER_HOST` (`3.16.167.114`, no Elastic IP -- update on any host resize, same gotcha that's bitten every app on this host at least once), `GARMIN_VIEWER_DEPLOY_KEY` (dedicated key, not shared with other repos' deploy keys). The nginx vhost itself is not managed by this workflow -- it's a hand-edited host file, same as every other vhost on this box.
  - Replaces the old `deploy-ec2.yml` / `EC2_*` secrets, which synced this repo to `/home/ubuntu/garmin` -- a directory nothing actually served (the real `/garmin` route on the main site ran from `aws_flask_site`'s own submodule checkout, deployed by that repo's own CI). Both the stale workflow and its secrets were deleted 2026-08-01.
  - Reads S3 only: `GARMIN_VIEWER_SOURCE=s3` (plus `GARMIN_S3_BUCKET=my-garmin-data`, `GARMIN_RUNTIME_ENV=aws`) set via the systemd unit's `Environment=` lines, which `garmin/app/routes.py`'s `DEFAULT_SOURCE` reads so every route defaults to S3 without needing `?source=s3` on every request. AWS credentials come from the host's existing instance profile (`ec2-kaya-viewer-role`, extended additively with a `garmin-viewer-readonly` inline policy granting `s3:GetObject` on all of `my-garmin-data/*` -- started narrower, scoped to `curated/*`+`viewer_cache/*`, but broadened after the (now-deleted) legacy Bokeh route needed the `dashboards/*` prefix too; `s3:ListBucket` was already covered by `aws-monitor-readonly`'s wildcard prefix) -- **never create a competing instance profile**, this host only supports one at a time.
  - Cloudflare DNS: proxied A record, same pattern as `kaya`/`aws_monitor` (zone `08206cb111c3f96b54ecb3db3e5ccead`, `CLOUDFLARE_API_TOKEN` in local shell env, not this repo).
  - Integration into the main site (`peterwilliams.dev/garmin` iframing this subdomain, replacing the submodule blueprint registration in `aws_flask_site`) is `aws_flask_site`'s own work, not done from here -- see that repo.
- Analyzer Lambda's CI deploy (`deploy-lambda-analyzer.yml`) was fixed 2026-08-01 -- `garmin-app` lacked any ECR permissions at all (`AWSLambda_FullAccess` covers Lambda but not ECR push/auth). Added inline policy `garmin-app-ecr-deploy`: account-wide `ecr:GetAuthorizationToken` (required by ECR, can't be resource-scoped) plus push actions (`BatchCheckLayerAvailability`/`InitiateLayerUpload`/`UploadLayerPart`/`CompleteLayerUpload`/`PutImage`/`BatchGetImage`/`GetDownloadUrlForLayer`) scoped to just the `garmin-data-analyzer` repo ARN. Verified via a real `gh run rerun` -> success -> Lambda `LastModified` bump. The manual `docker buildx build --push` path (see Useful Commands below) still works and is still fine to use for local iteration before committing.
- RDS: legacy migration input only, not the target store. Populated tables are in the `public` schema; the `garmin` schema is effectively empty.
- AWS account `545009868532`, preferred operator profile `admin` (`arn:aws:iam::545009868532:user/pw-admin-cli`), region `us-east-2`. Profiles `personal`/`garmin` also exist; retry under `admin` if a command fails under `garmin`.

## Useful commands

```bash
# identity / inventory
aws sts get-caller-identity --profile admin --region us-east-2
aws s3 ls s3://my-garmin-data --profile admin --region us-east-2
aws lambda get-function-configuration --function-name garmin-data-updater --profile admin --region us-east-2

# S3 objects
aws s3 cp s3://my-garmin-data/<key> ./tmp/<filename> --profile admin --region us-east-2
aws s3 sync ./local-dir s3://my-garmin-data/<prefix>/ --profile admin --region us-east-2

# Lambda
aws lambda invoke --function-name garmin-data-updater --profile admin --region us-east-2 /tmp/garmin-lambda-response.json
aws logs describe-log-streams --log-group-name /aws/lambda/garmin-data-updater --order-by LastEventTime --descending --max-items 10 --profile admin --region us-east-2

# Analyzer Lambda (manual invoke + tail logs)
aws lambda invoke --function-name garmin-data-analyzer --profile admin --region us-east-2 /tmp/analyzer-response.json
aws logs tail /aws/lambda/garmin-data-analyzer --profile admin --region us-east-2 --since 10m

# Rebuild + push the analyzer image manually (CI does this on push to prod;
# use this for local iteration before committing)
docker buildx build --platform linux/amd64 --provenance=false --sbom=false \
  -f infra/docker/analyzer.Dockerfile -t 545009868532.dkr.ecr.us-east-2.amazonaws.com/garmin-data-analyzer:latest \
  --push .
aws lambda update-function-code --function-name garmin-data-analyzer \
  --image-uri 545009868532.dkr.ecr.us-east-2.amazonaws.com/garmin-data-analyzer:latest \
  --profile admin --region us-east-2
```

Note: `--provenance=false --sbom=false` are required on the image build -- Docker Buildx attaches provenance/SBOM attestations by default, producing an OCI manifest list that Lambda's `CreateFunction`/`UpdateFunctionCode` rejects with `InvalidParameterValueException` ("image manifest ... not supported").

For legacy RDS inspection, use the existing machine-local `rds-tunnel` SSH alias rather than the AWS CLI.

## Guardrails

- Keep credentials only in `~/.aws` or AWS secret stores — never in repo files.
- Prefer AWS CLI over console clicking for repeatable inspection.
- Don't reintroduce VPC/NAT for this Lambda without a concrete new dependency that needs it.
- Don't put Garmin username/password into recurring Lambda runtime config — local bootstrap + Secrets Manager refresh is the model (see `README.md`).
