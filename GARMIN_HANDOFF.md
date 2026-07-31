# Garmin Repo — AWS/Ops Reference

Deeper reference for AWS state and CLI operations. Start with `CLAUDE.md` for repo architecture/rules; this file is for when you actually need to touch AWS.

## What this repo owns vs. doesn't

Owns: Garmin Connect access/session management, data pull/update/processing logic, private storage outputs and dashboard artifacts.

Doesn't own: the public website shell, cross-repo deployment topology. If a change affects how `aws_flask_site` integrates Garmin outputs, call that out rather than assuming both repos are open.

## Verified live state

- Lambda: `garmin-data-updater`, region `us-east-2`, runtime `python3.11`, handler `garmin.scripts.lambda_update.lambda_handler`, timeout `900s`, memory `512 MB`, no VPC attachment, role-based S3 access (no bucket-policy workaround).
  - Env: `GARMIN_USE_AWS_SECRETS=1`, `GARMIN_AWS_SECRET_NAME=garmin/oauth2_token`, `GARMIN_S3_BUCKET=my-garmin-data`.
  - CloudWatch log group: `/aws/lambda/garmin-data-updater`.
  - Was previously misconfigured at 128MB and timing out; fixed 2026-07 by raising to 512MB. If timeouts recur, check memory/duration in the `REPORT` log line before assuming an auth regression.
- S3 buckets: `my-garmin-data` (curated parquet + artifacts), `my-garmin-config`.
- Secrets Manager: `garmin/oauth2_token` (OAuth2 refresh token; OAuth1 secret is derived by name, `oauth1_token`).
- Curated S3 layout: `curated/daily/<dataset>.parquet`, `curated/detailed/<dataset>/query_date=YYYY-MM-DD.parquet`, `curated/metadata/detailed_status/<dataset>.parquet`, `curated/analyzed/<dataset>/...`. Legacy prefixes `processed/`, `moving_averages/`, `dashboards/` still exist and should be retired intentionally, not by accident.
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
```

For legacy RDS inspection, use the existing machine-local `rds-tunnel` SSH alias rather than the AWS CLI.

## Guardrails

- Keep credentials only in `~/.aws` or AWS secret stores — never in repo files.
- Prefer AWS CLI over console clicking for repeatable inspection.
- Don't reintroduce VPC/NAT for this Lambda without a concrete new dependency that needs it.
- Don't put Garmin username/password into recurring Lambda runtime config — local bootstrap + Secrets Manager refresh is the model (see `README.md`).
