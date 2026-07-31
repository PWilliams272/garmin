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

**The full pull → analyze → viewer-cache pipeline is now scheduled end to end**, not just the pull: `garmin-data-updater` (pull, 3:00 UTC) → `garmin-data-analyzer` (analysis + viewer-cache build, container-image Lambda since scipy/scikit-learn/statsmodels don't fit a zip Lambda's 250MB limit, 3:30 UTC). This is what makes the deployed app fast against S3 (one cached JSON read per page instead of a dozen+ individual S3 reads live). Remaining work: keep monitoring scheduled runs, watch for real auth failures vs. infra timing, continue packaging cleanup, and backfill `curated/activities/` (running/strength/lifting) to S3 -- it currently only exists locally, so those viewer-cache entries are skipped and the deployed app's Fitness/Activities pages still serve mock data.

## Goal 2 — Workouts/Activities: Running + Strength Done, Multi-Sport In Progress

**Running and strength training are done**, end to end: `ActivityPuller` pulls both from Garmin, `curated_store.py` has a dedicated `curated/activities/summary|detail/` shape (one row per activity + a per-activity detail table for strength sets), wired into `DataUpdater.update_all()`, with GP-trend analysis for running metrics and a real (non-mock) Fitness tab. 1062 runs and 490 strength sessions are backfilled locally as of 2026-07-31.

**Still open** (actively being worked, see the plan that produced 2026-07-31's commits for the full breakdown):
- Multi-sport summary data — cycling, swimming, hiking, climbing, etc. — generalizing `ActivityPuller`/`updaters.py`'s activity loop from two bespoke methods into one registry-driven loop, to replace the Activities tab's mock overview.
- Better lifting analysis — currently just a Gaussian moving average over 3 hardcoded exercises' top-set weight; needs exercise discovery, 1RM/volume tracking, and the same STS trend treatment already built for Health metrics.
- A per-activity GPS/detail drill-in (replacing `_mock_activity_detail`) is still unstarted — needs new Garmin endpoint research (splits/laps/polyline), deferred beyond a one-activity sample pull for map prototyping.
- Legacy DB-backed paths and legacy S3 prefixes (`processed/`, `moving_averages/`, `dashboards/`) are still due for intentional retirement — not yet done.

## Goal 3 — Bokeh → Plotly Migration

**Health tab: done.** `/quick_dashboard` is a new kaya-style Plotly app — precomputed chart-ready JSON served by `/api/quick_dashboard_data`, no server-side rendering, no live model fitting (the GP/STS trend models run offline via `manual_analyze_metrics.py`). All 6 health metrics (weight, body fat, muscle mass, bone mass, resting HR, steps) have their own collapsible chart card with quality-tiered points, an STS trend with predictive bands, a residual strip + histogram, and a stat summary (last reading, trend now, typical day-to-day variability, 14/30/60-day trend arrows).

**Not yet migrated**: `/metrics_dashboard` and `/curated_metrics_dashboard` are still the old Bokeh routes (`src/garmin/dashboard_curated.py`, `src/garmin/analysis/plotting.py`, `src/garmin/analysis/analysis.py`, templates `curated_metrics_dashboard.html`/`metrics_dashboard.html`). Fitness/Activities/Analytics tabs on the new app exist as UI shells with mock data — real activity ingestion (Goal 2) is the blocker for making those real. Once Goal 2 lands and those tabs are real, retire the old Bokeh routes/templates rather than running both indefinitely.

## Goal 4 — `garmin.peterwilliams.dev`, Matching Kaya's Pattern Exactly

**This means deprecating the current integration, not adding a second one.** Garmin is currently embedded in `aws_flask_site` as a git submodule (`garmin/`, pinned to `prod`) with its blueprint registered at `/garmin` via `app/integrations/project_surfaces.py`. The kaya-style pattern replaces this entirely: an independent service on its own subdomain, iframe-embedded into the main site — not a Flask blueprint living inside `aws_flask_site`'s process. Full concrete setup spec is in `WEB_APP_SETUP.md` in this repo (mirrors the same doc already written for `kaya` and `ticket_price_tracker`, for consistency across all three).

**Don't start Goal 4 before Goals 2-3 are far enough along** that there's real curated activity data and Plotly-based chart code to actually serve — building the subdomain/deploy plumbing first with nothing real to show it would be backwards, same sequencing logic as the other two portfolio projects.

## Goal 5 — Predictive/Data Science Analysis

Open-ended, not scoped in detail here — genuinely depends on what Goal 2's expanded dataset (especially real activity data) makes possible. Worth revisiting once activities are flowing; there's likely more interesting analysis possible with structured workout data (training load, recovery patterns relative to activity intensity, etc.) than with the health-metrics-only dataset that exists today.

## Suggested Sequencing

1. Finish Goal 1's monitoring/verification (already mostly done, just confirm scheduled runs stay healthy).
2. Goal 2: design and build activities/workouts support, retire legacy DB/S3 paths along the way.
3. Goal 3: health metrics done; finish once Goal 2 gives the Fitness/Activities tabs real data to replace their mocks, then retire the old Bokeh routes.
4. Goal 4: build and deploy the subdomain app once 2-3 give it something real to show; deprecate the submodule integration in `aws_flask_site` at the same time, don't run both indefinitely.
5. Goal 5: pursue once the richer dataset (especially activities) exists.
