# Garmin Repo — AWS/Ops Reference

Deeper reference for AWS state and CLI operations. Start with `CLAUDE.md` for repo architecture/rules; this file is for when you actually need to touch AWS.

## What this repo owns vs. doesn't

Owns: Garmin Connect access/session management, data pull/update/processing logic, private storage outputs and dashboard artifacts.

Doesn't own: the public website shell, cross-repo deployment topology. If a change affects how `aws_flask_site` integrates Garmin outputs, call that out rather than assuming both repos are open.

## Verified live state

- Lambda: `garmin-data-updater` (pull only), region `us-east-2`, runtime `python3.11`, handler `garmin.scripts.lambda_update.lambda_handler`, timeout `900s`, memory `512 MB`, no VPC attachment, role-based S3 access (no bucket-policy workaround), zip-packaged, deployed via `.github/workflows/deploy-lambda.yml` on push to `prod`.
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
- As of 2026-07-31, `curated/activities/` (running/strength/lifting data) exists **locally only** — never backfilled to S3. Run `python -m garmin.scripts.manual_update --storage-target s3` (needs a live Garmin session) to populate it there; until then `fitness_running_s3`/`fitness_lifting_s3`/`activities_overview_s3` viewer-cache entries are skipped and those pages fall back to mock data in the deployed app.
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
