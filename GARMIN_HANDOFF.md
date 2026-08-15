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

## Analyzer Lambda outage, 2026-07-31 → 2026-08-14

**The analyzer Lambda failed on every scheduled run for two weeks**, and nothing
alerted. Found 2026-08-14 while checking whether a curated-data repair had
propagated downstream.

```
[ERROR] Runtime.ImportModuleError: Unable to import module
        'garmin.scripts.lambda_analyze': No module named 'fitparse'
```

It failed at *import*, so it never ran a line of analysis. `curated/daily/` stayed
current (that's the other Lambda) while everything the analyzer writes —
`curated/analyzed/`, the viewer cache — froze at 2026-07-31. The viewer served
two-week-old analysed data with no outward sign of a problem, because the fresh
daily data underneath it kept updating.

**Cause.** `fitparse` is in `pyproject.toml` but not in
`src/garmin/requirements-analyzer.txt`, so the container never had it. The
analyzer doesn't parse FIT files; the dependency arrived through this chain:

```
lambda_analyze:14 -> manual_build_viewer_cache:15 -> model_report:25
                  -> updaters:12 -> pullers/activities:6 -> fitparse
```

`model_report.py` imported `ACTIVITY_DATASETS` — a list of twelve strings — from
`updaters`, which pulls in the whole Garmin pulling stack.

**Fix.** The constant moved to `garmin/datasets.py`, which imports nothing;
`updaters` re-exports it for existing callers. Adding `fitparse` to the analyzer
requirements would also have worked, but would have shipped a FIT parser to a job
that never opens one.

**Why no test caught it:** the local venv has `fitparse`, so every test and every
manual run passed. Only the container was short. `tests/test_analyzer_import_surface.py`
now imports each analyzer entrypoint in a subprocess with the package hidden —
verified to fail against the old import and pass against the new.

**Resolved 2026-08-14 13:44 UTC-7.** Image rebuilt and deployed; the function ran
clean — 312s, 270MB of 1024MB, no errors — and rewrote every analyzed artifact
and all 7 viewer-cache files. Verified `analyzed/health_stats/body_fat_points`
now has 0 zeros and a 17.7–21.5% range, so the curated repair flowed through.

The deploy was **scoped to the fix only**: built from `deploy/analyzer-import-fix`
(= the previously-live commit `47164be` plus the two fix commits), not from the
feature branch, which is 9 commits further along and carries unreleased modelling
changes. Two notes for whoever deploys next:

- **`prod` is not the deploy source for this Lambda.** The `prod` branch sits at
  `c133608 "Add ec2 deployment"`, which is not even an ancestor of the deployed
  image's commit. Images are built from the working tree by the manual command
  above. Don't assume `prod` reflects what is live — check the image tag.
- **Init times out at 10s** (`INIT_REPORT ... Phase: init Status: timeout`) because
  importing pandas/scipy/sklearn/statsmodels exceeds Lambda's init budget. Lambda
  retries the init inside the invoke phase and the run then succeeds, so this is
  survivable and pre-existing — but it is noise in the logs that looks like a
  failure, and it makes every run pay the init cost twice.

**Two gaps this leaves open** (neither addressed):

- Nothing alerts on a failing scheduled Lambda. A two-week silent outage should
  not depend on someone noticing a stale timestamp. `aws_monitor` is the natural
  home.
- `requirements-analyzer.txt` is maintained by hand and can drift from what the
  code actually imports. The new test covers the analyzer's entrypoints, not the
  general problem.

## Garmin already computes strain, load and max HR — we don't pull any of it

Probed live 2026-08-15. All of the following work against the current session and
**none of it is in any puller today**. Before hand-rolling a training-load metric,
note that Garmin ships a validated EPOC-based one.

**Per activity** — already in the `/activitylist-service/.../activities` response
the summary pullers already call, just not mapped:

| field | example | note |
| --- | --- | --- |
| `activityTrainingLoad` | 38.4 | Garmin's EPOC-based load — the real TRIMP equivalent |
| `aerobicTrainingEffect` / `anaerobicTrainingEffect` | 1.6 / 2.0 | 0–5 scale |
| `hrTimeInZone_1..5` | 1203, 1122, 256, 0, 0 s | **makes Edwards zone-TRIMP free** — no zone maths needed |
| `moderateIntensityMinutes` / `vigorousIntensityMinutes` | 38 / 14 | |
| `maxHR` | 154 | per activity |

Adding these is a mapping change in `pull_cardio_summary` / `pull_running_summary`
/ `pull_strength_summary` plus a summary backfill — no new endpoint.

**Max HR and zones** — `/biometric-service/heartRateZones`:
`maxHeartRateUsed` **197**, `lactateThresholdHeartRateUsed` 171, zone floors
100/116/136/160/177, and a separate set per sport. This is what Banister TRIMP
needs, alongside the resting HR already in `heart_rate.parquet`. Garmin's 197 is
observed, and notably higher than the age-predicted ~187 — so use this rather than
a formula.

**Daily readiness** — `/metrics-service/metrics/trainingreadiness/{date}` returns
`acuteLoad`, `acwrFactorPercent`, `recoveryTime`, `hrvWeeklyAverage`, `score`,
`sleepScore`, and per-factor feedback. **Garmin computes its own acute load and
ACWR**, which `daily_panel` currently re-derives from duration and average HR.
Worth comparing the two rather than assuming ours is better.

**VO2max works and has history** — `/metrics-service/metrics/maxmet/daily/{start}/{end}`
returns **266 records from 2023-01-05**, per sport (`cycling`, `generic`). This is
the endpoint already sitting unverified in `health.py`; it is correct. Note a
short recent window can return `[]` because VO2max only updates on qualifying
activities, so don't conclude it's broken from one empty response.

**Training status** — `/metrics-service/metrics/trainingstatus/aggregated/{date}`
gives `mostRecentTrainingLoadBalance` (monthly aerobic-low / aerobic-high /
anaerobic load against target ranges) and heat/altitude acclimation.

## Known data-quality issues

Verified against live S3 on 2026-08-14. These are properties of the curated
data, not of the code reading it — check here before chasing a weird result.

| Field | Issue | Status |
| --- | --- | --- |
| `health_stats.bmi` / `.body_fat` / `.fat_mass` | 43 days (2019-10-31 → 2020-03-06) held `0.0` instead of null | **Root cause fixed**; historical rows need the repair script below |
| `sleep.skin_temp_f` / `.skin_temp_c` | null for all 3580 rows | Open — see below |
| `health_stats.weight` | 33.9% missing since 2023, incl. a 194-day gap (2023-07-07 → 2024-01-16) | Not a bug — behavioural, see below |
| `activities/summary/running.start_time` | was null for 1061 of 1062 rows | **Fixed 2026-08-14** — backfilled, 0 null |
| `activities/summary/strength.start_time` | was null for 489 of 490 rows | **Fixed 2026-08-14** — backfilled, 0 null |
| `rock_climbing` / `pickleball` / `hiit` details | none exist | **Not fixable** — manually logged, see below |

**Manually-logged sessions have no detail and never will.** All 7 HR-era
`rock_climbing`, `pickleball` and `hiit` activities have `avg_hr` null and return
an empty detail fetch: they are 4–6 hour climbing days entered by hand after
forgetting to record. Not a backfill gap. Downstream should treat these as
*sessions with missing HR* and impute, which is why `daily_panel` now emits
`sessions_missing_hr` and `duration_missing_hr` (see below) — without those, a
hand-logged session and a rest day are both `hr_load == 0` and indistinguishable.

### Per-second HR exists only from 2022-12-04 — this is a device boundary, not a gap

`heart_rate_bpm` in `curated/activities/detail/*_timeseries/` looks alarmingly
patchy per sport (cycling 7.5% of files, running 21.3%, bouldering 100%). **That
is an artifact of each sport's age distribution, not a pull failure.** Full census
of all 2898 local detail files, by year:

| year | ≤2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
| --- | --- | --- | --- | --- | --- | --- |
| files with HR | **0%** | 5.6% | **100%** | **100%** | **100%** | **100%** |

The boundary is three days wide: last activity without HR is **2022-12-01**, first
with HR is **2022-12-04**. That is the Fenix 7 coming online, and it matches the
daily wellness datasets exactly — `hrv` starts 2022-12-04, `heart_rate`, `stress`,
`body_battery` and `respiration` all start 2022-12-03.

So bouldering is 100% because it only *starts* in 2023-01; cycling is 7.5% because
973 of its 1050 files predate the watch. Cycling since 2023 is 77 of 77. **No
backfill can recover pre-December-2022 per-second HR — it was never recorded.**

Practically this costs nothing: any model needing HRV or sleep score is confined
to the same window anyway. Don't let a per-sport coverage table start a hunt for a
pull bug that isn't there.

### Power is not one signal — running power is estimated, cycling power is intermittent

`power_w` in the detail timeseries has two completely different provenances, and
nothing in the data distinguishes them. Treating them as one column will silently
mix an estimate with a measurement.

**Running power is always watch-estimated.** It appears on 225 of 1054 files, and
its first appearance is **2022-12-04** — the same day as HR, i.e. the Fenix 7. So
it is 100% of the HR era and 0% before, produced by the watch's own model rather
than any sensor. There is no true running power in this dataset and never will be
without a footpod.

**Cycling power comes from a crank power meter and switches on and off:**

| period | files with power |
| --- | --- |
| before 2023-01 | 0% (none at all) |
| 2023-01 → 2023-11 | 100% |
| 2024-03 → 2025-02 | **0%** |
| 2025-03 onward | mostly present |

Mean power is comparable across both on-periods (~160–215 W), so this is not an
estimated-then-measured transition — it reads as the meter being present, then
absent for about a year, then present again. Most likely a second bike without the
meter, or a dead battery. **The date the meter was added is not recoverable from
the curated data**; the gap structure is, and it is what matters for modelling.

Practical guidance: do not build a feature that assumes a single "power meter
added" cutover. Condition on `power_w` being present per activity, and never pool
running power with cycling power.

### The pattern behind most of these

**Three of the four known issues share one root cause: a puller improvement only
ever reaches newly-pulled rows.** Both `_update_daily_time_series_curated` and
`_update_activity_curated` resume from the last stored date, so when a column is
added or a parsing bug fixed, every existing row keeps the old schema forever and
nothing in the daily loop revisits it.

When changing a puller's *output schema* — not just its behaviour — assume you
also need a backfill, or the change silently applies to a thin sliver of recent
data while the history it is meant to fix stays broken.

**`start_time` on running/strength** is the clearest instance. It was added to
`pull_running_summary` and `pull_strength_summary` in `937e27a` (2026-07-31); by
then both datasets held years of history, so only activities recorded *after*
that date have it — exactly one each. The sports handled by
`pull_cardio_summary`, introduced the same day in `7025b0d` and therefore pulled
fresh, are 100% populated. Same code, same field; the difference is entirely
whether history predated the change.

`merge_activity_summary` de-duplicates on `activity_id` keeping the last row, so
re-pulling overwrites cleanly:

```bash
# report only -- needs no Garmin credentials, contacts nothing
python -m garmin.scripts.manual_backfill_activity_summaries \
    --storage-target s3 --report-only
python -m garmin.scripts.manual_backfill_activity_summaries \
    --storage-target s3 --dataset running --apply
```

`--apply` needs Garmin credentials and re-pulls the full activity list, which is
rate-limited — do one dataset at a time.

**Run 2026-08-14 (backed up to `backups/2026-08-14-pre-start-time-backfill/`).**
Both datasets are now fully populated: running 1062/1062, strength 490/490, row
counts and unique `activity_id` counts unchanged, so the merge overwrote rather
than duplicated. Auth came from the stored OAuth token via
`GARMIN_USE_AWS_SECRETS=1 GARMIN_AWS_SECRET_NAME=garmin/oauth2_token`; no
username/password needed.

Values were checked rather than assumed: zero disagreements between
`start_time`'s date and the `date` column, all 60 minute and 60 second values
represented, no clustering at midnight or noon, and a stable median start hour
(14–19) across every year from 2015. These are genuine recorded timestamps, not
synthesized placeholders — which matters, because an old activity carrying a
fabricated noon would silently corrupt any time-of-day analysis.

A transient `botocore ConnectionClosedError` surfaced mid-run on the strength
pass; boto retried and the write completed correctly. Verify integrity after any
such error rather than assuming either outcome.

**Weight coverage is behavioural, not a dropped-reading bug.** Tested by
day-of-week: since 2025, Saturday 22.6% and Sunday 23.8% missing against
Wednesday 10.6% — roughly double at weekends. A puller silently dropping
readings has no way to know what day of the week it is. Streaks back this up:
34 single missing days, longest run 9 days. This is someone not standing on the
scale, not a pipeline fault.

**Composition zeros.** Garmin returns `0` for body-composition fields when a
scale reports only a weight. Zero is physiologically impossible for all of them,
so `HealthPuller._post_process_weight` now nulls them before the daily groupby —
before matters, since averaging a `0` with a real reading on the same day yields
a plausible-looking wrong number. `fat_mass` is derived from `body_fat` and
inherited the placeholder.

The fix only affects newly-pulled days: `_update_daily_time_series_curated`
resumes from the last stored date and never revisits history. Repair the
existing rows with:

```bash
# dry run first -- reports counts, writes nothing
python -m garmin.scripts.manual_repair_health_stats_zeros --storage-target s3
python -m garmin.scripts.manual_repair_health_stats_zeros --storage-target s3 --apply
```

Lossless by construction, but it writes to production S3 — confirm before `--apply`.

**Skin temperature.** Both the `_f` and `_c` variants are null across every row,
while other fields mapped from the same `/sleep-service/stats/sleep/daily/`
response (`sleep_need` 905, `body_battery_change` 978, `spo2` 1296) populate
normally. So this is **not** a mapping typo or a unit-field mix-up — that
endpoint does not carry skin temperature at all, and the `skinTempF`/`skinTempC`
keys in the sleep mapping have never matched anything. Fixing it means finding
the right endpoint, not renaming a key. The column is carried into
`analyzed/panel/daily_panel.parquet` as a dead column; don't build on it.

**Weight coverage.** Missing by year: 2023 71%, 2024 26%, 2025 14%, 2026 18%.
Almost all of 2023 is one 194-day streak. Body composition begins 2024-04-12 for
all six fields at once, consistent with a scale change. Treat weight as unusable
as a covariate for 2023, and note the missingness is unlikely to be random —
not weighing in correlates with the kind of period you would most want to
measure.

## Guardrails

- Keep credentials only in `~/.aws` or AWS secret stores — never in repo files.
- Prefer AWS CLI over console clicking for repeatable inspection.
- Don't reintroduce VPC/NAT for this Lambda without a concrete new dependency that needs it.
- Don't put Garmin username/password into recurring Lambda runtime config — local bootstrap + Secrets Manager refresh is the model (see `README.md`).
