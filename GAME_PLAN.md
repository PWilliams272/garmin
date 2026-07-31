# Garmin — Next Phase Game Plan

Complements `GARMIN_HANDOFF.md` (AWS/ops reference) and `CLAUDE.md` (architecture/rules) — this file covers the next phase of work: five goals from the project owner, going beyond keeping the current pipeline healthy.

## The Five Goals, In Order

1. **Clean up code and data; confirm data is flowing and Lambdas are working.**
2. **Clean up data processing; add more data / figure out how to process other data components (e.g. workouts).**
3. **Migrate Bokeh plots to Plotly (or something lighter), in a form that supports a new app like `kaya`'s.**
4. **Deploy that app at `garmin.peterwilliams.dev`, integrated into `aws_flask_site` — same protocol/style as `kaya`.**
5. **More predictive/data science analysis.**

## Goal 1 — Already Substantially Done

See `GARMIN_HANDOFF.md` for the verified live state: the Lambda's 128MB→512MB timeout fix is deployed and verified, a state-leak bug in the detailed puller is fixed, and detailed timing logs are live. Remaining work: keep monitoring scheduled runs, watch for real auth failures vs. infra timing, and continue packaging cleanup. The 8 curated datasets (`health_stats`, `steps`, `sleep`, `stress`, `body_battery`, `heart_rate`, `hrv`, `respiration`) are confirmed actively updating daily as of 2026-07-29.

## Goal 2 — Workouts/Activities: Confirmed Genuinely Unstarted

Checked directly: `src/garmin/pullers/activities.py` exists but is a 20-line skeleton (`ActivityPuller` with `pull_list()` and `get_strength_workout()` — the latter only handles strength-training exercise sets, not general cardio/GPS-based activities like runs or rides). It's **explicitly disabled** — `src/garmin/updaters.py` has `#self.activity_puller = activity_puller or ActivityPuller(session)` commented out. It's not wired into `update_all()`, not in the curated S3 output, nothing downstream consumes it.

To actually build this out:
- Decide scope: which activity types matter first (running/cycling with GPS+pace+splits, strength sets, or both)? The existing `get_strength_workout` only covers one of these.
- Design the curated schema for activities (likely its own `curated/activities/` prefix, probably one row per activity plus a detail table for splits/sets, following the existing `curated/daily/` + `curated/detailed/<dataset>/` pattern already established for other data types — don't invent a third storage shape).
- Wire it into `DataUpdater.update_all()` once built, following the same pattern as the other curated updaters in `src/garmin/updaters.py`.
- Alongside adding activities: the existing handoff already flags legacy DB-backed paths and legacy S3 prefixes (`processed/`, `moving_averages/`, `dashboards/`) as due for intentional retirement — worth doing this cleanup while touching the same code, not as a separate later pass.

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
