# Garmin — Next Phase Game Plan

Complements `GARMIN_HANDOFF.md` (AWS/ops reference) and `CLAUDE.md` (architecture/rules) — this file covers the next phase of work: five goals from the project owner, going beyond keeping the current pipeline healthy.

## The Five Goals, In Order

1. **Clean up code and data; confirm data is flowing and Lambdas are working.**
2. **Clean up data processing; add more data / figure out how to process other data components (e.g. workouts).**
3. **Migrate Bokeh plots to Plotly (or something lighter), in a form that supports a new app like `kaya`'s.**
4. **Deploy that app at `garmin.peterwilliams.dev`, integrated into `aws_flask_site` — same protocol/style as `kaya`.**
5. **More predictive/data science analysis.**

## Goal 1 — Already Substantially Done

See `GARMIN_HANDOFF.md` for the verified live state: the Lambda's 128MB→512MB timeout fix is deployed and verified, a state-leak bug in the detailed puller is fixed, and detailed timing logs are live. The 8 curated datasets (`health_stats`, `steps`, `sleep`, `stress`, `body_battery`, `heart_rate`, `hrv`, `respiration`) are confirmed actively updating daily as of 2026-07-29.

**The full pull → analyze → viewer-cache pipeline is now scheduled end to end**, not just the pull: `garmin-data-updater` (pull, 3:00 UTC) → `garmin-data-analyzer` (analysis + viewer-cache build, container-image Lambda since scipy/scikit-learn/statsmodels don't fit a zip Lambda's 250MB limit, 3:30 UTC). This is what makes the deployed app fast against S3 (one cached JSON read per page instead of a dozen+ individual S3 reads live).

**2026-08-01: `garmin-data-updater` was found stuck on 2026-07-06-era code** (nobody had pushed `prod` in the intervening weeks despite substantial work landing on feature branches) — redeployed. `curated/activities/` (all 12 sports, not just running/strength/lifting) is now backfilled to S3, including FIT-based per-point detail, and `_activity_type_registry()` now wires detail-pulling for every cardio sport into the daily scheduled run (previously only strength had a `detail_fn`; FIT pulling existed as a script but was never in the registry). See `GARMIN_HANDOFF.md` for the full breakdown. Remaining work: keep monitoring scheduled runs, watch for real auth failures vs. infra timing, continue packaging cleanup, and fix the analyzer Lambda's CI deploy (blocked on an ECR IAM permission gap for the `garmin-app` CI user — manual deploys still work).

## Goal 2 — Workouts/Activities: Running + Strength Done, Multi-Sport In Progress

**Running, strength, and multi-sport are all done**, end to end: `ActivityPuller` pulls 12 activity types from Garmin via a registry-driven loop (`_activity_type_registry()`/`_update_activity_curated()` in `updaters.py`), `curated_store.py` has a dedicated `curated/activities/summary|detail/` shape, wired into `DataUpdater.update_all()`. Every cardio sport also gets real FIT-based per-point detail (map, pace, HR, cadence, power) via `get_activity_detail_timeseries`, both for new activities on the daily schedule and via a resumable one-time backfill (`manual_backfill_activity_details.py`) for history that predates the registry wiring. GP-trend analysis covers running and every other cardio sport; the Activities tab is a real browsable list + per-activity detail view, not a mock shell. All backfilled to S3 as of 2026-08-01.

**Still open**:
- Better lifting analysis — currently just a Gaussian moving average over 3 hardcoded exercises' top-set weight; needs exercise discovery, 1RM/volume tracking, and the same STS trend treatment already built for Health metrics.
- Legacy DB-backed paths and legacy S3 prefixes (`processed/`, `moving_averages/`, `dashboards/`) are still due for intentional retirement — not yet done.

## Goal 3 — Bokeh → Plotly Migration

**Health tab: done.** `/quick_dashboard` is a new kaya-style Plotly app — precomputed chart-ready JSON served by `/api/quick_dashboard_data`, no server-side rendering, no live model fitting (the GP/STS trend models run offline via `manual_analyze_metrics.py`). All 6 health metrics (weight, body fat, muscle mass, bone mass, resting HR, steps) have their own collapsible chart card with quality-tiered points, an STS trend with predictive bands, a residual strip + histogram, and a stat summary (last reading, trend now, typical day-to-day variability, 14/30/60-day trend arrows).

**Not yet migrated**: `/metrics_dashboard` and `/curated_metrics_dashboard` are still the old Bokeh routes (`src/garmin/dashboard_curated.py`, `src/garmin/analysis/plotting.py`, `src/garmin/analysis/analysis.py`, templates `curated_metrics_dashboard.html`/`metrics_dashboard.html`). Fitness/Activities/Analytics tabs on the new app exist as UI shells with mock data — real activity ingestion (Goal 2) is the blocker for making those real. Once Goal 2 lands and those tabs are real, retire the old Bokeh routes/templates rather than running both indefinitely.

## Goal 4 — `garmin.peterwilliams.dev` — Deployed 2026-08-01 (This Repo's Side Done)

The standalone viewer app is live: `garmin.peterwilliams.dev`, same shared EC2 host as `kaya`/`aws_monitor`, `garmin-viewer.service` on port `8030`, reading only S3 (`curated/`, `viewer_cache/`) via a new additive IAM policy on the shared instance role, gated with nginx HTTP Basic Auth (not the main-site shared-session pattern -- see `GARMIN_HANDOFF.md` for why), Cloudflare DNS live, CI deploy workflow (`deploy-viewer-app.yml`) wired and verified. One deliberate deviation from `WEB_APP_SETUP.md`: kept the existing Flask app instead of rewriting to FastAPI, since it was already fully built and tested — gunicorn instead of uvicorn is the only real consequence.

**What's NOT done from here**: replacing the `aws_flask_site` submodule blueprint at `/garmin` with a login-gated iframe route (the actual "deprecate the current integration" step). That's `aws_flask_site`'s own work in its own repo, not something this repo pushes to. Until that lands, `aws_flask_site` still serves `/garmin` from its submodule (stale, pinned to whatever commit the submodule pointer references) alongside the new independent `garmin.peterwilliams.dev`.

## Goal 5 — Predictive/Data Science Analysis

Open-ended, not scoped in detail here — genuinely depends on what Goal 2's expanded dataset (especially real activity data) makes possible. Worth revisiting once activities are flowing; there's likely more interesting analysis possible with structured workout data (training load, recovery patterns relative to activity intensity, etc.) than with the health-metrics-only dataset that exists today.

## Suggested Sequencing

1. Finish Goal 1's monitoring/verification (already mostly done, just confirm scheduled runs stay healthy).
2. Goal 2: design and build activities/workouts support, retire legacy DB/S3 paths along the way.
3. Goal 3: health metrics done; finish once Goal 2 gives the Fitness/Activities tabs real data to replace their mocks, then retire the old Bokeh routes.
4. Goal 4: build and deploy the subdomain app once 2-3 give it something real to show; deprecate the submodule integration in `aws_flask_site` at the same time, don't run both indefinitely.
5. Goal 5: pursue once the richer dataset (especially activities) exists.
