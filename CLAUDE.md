# garmin

Pulls, processes, and visualizes personal Garmin Connect data. Python 3.11, Flask, pandas, Plotly, boto3.

## Architecture

Layered pipeline, each stage only reads the previous stage's output — nothing computes on request:

- `garmin/api/` — Garmin Connect session/auth boundary. Runtime code does not use `garth`; keep provider-specific logic isolated here.
- `garmin/pullers/` — fetch raw data from Garmin.
- `garmin/updaters.py` — orchestrates pulls, writes curated output.
- `garmin/io/` — `file_manager.py` (local-vs-S3 abstraction), `curated_store.py` (curated parquet read/write), `db_manager.py` (legacy DB path, being retired).
- `garmin/analysis/` — quality classification (`quality.py`), trend fitting (`trend_gp.py`, `trend_sts.py`), pipeline entrypoint (`analysis_pipeline.py`). Run offline via `garmin/scripts/manual_analyze_metrics.py`; the web app only ever reads the `curated/analyzed/` output. The multiscale GP fit is currently disabled (`FIT_GP_TREND = False`, memory-heavy and not rendered anywhere — see `GARMIN_HANDOFF.md`).
- `garmin/analysis/strength_curve.py` — hierarchical Bayesian load–repetition curves (PyMC). Fits `log(weight) ~ AsymmetricLaplace(alpha[e,t] - beta[e]*log(reps))` per exercise: `alpha` is a random walk over a **monthly knot grid** (`KNOT_SPACING_DAYS`) that sessions interpolate against, `beta` is a partially-pooled personal rep-decay exponent. The asymmetric (upper-envelope) likelihood is load-bearing, not stylistic — most sets are submaximal warmups, and a symmetric fit through the middle of that cloud returns a wildly biased β. Emits e1RM/e5RM/e8RM with credible intervals. **Sampling never runs in the daily analyzer Lambda** (memory + minutes); run `garmin/scripts/manual_fit_strength_curves.py` on its own cadence. `fit_strength_curves_for_all` refuses to persist a fit that fails its r-hat check.
- `garmin/analysis/model_report.py` — computes everything the `/modeling` explainer shows (load-type correlations with Fisher-z intervals, warmup-bias and quantile-sweep fits, rep distribution, HRV lag correlations, data coverage, the convergence-experiment log, and the literature-vs-bespoke provenance table) offline into the viewer cache. When a modelling decision is made, record the measurement that drove it here rather than only in prose.
- `garmin/scripts/manual_build_viewer_cache.py` — precomputes each page's full web-response JSON into `curated/viewer_cache/<page>_<source>.json`, reusing `garmin/app/routes.py`'s own payload-builder functions. Routes try this cache first (`routes._cached_or_live`), falling back to live assembly if missing. Run after `manual_analyze_metrics.py`.
- `garmin/app/` — Flask dashboard, Plotly-based (kaya-style). `/quick_dashboard` (Health), `/fitness` (running/lifting), `/activities` (browsable per-activity list + FIT-based map/pace/HR/cadence/power detail), `/data_status`, `/exercise_review` (accept/reject exercise-label corrections — the app's only write endpoints), `/modeling` (a paper-style explainer: plan, equations, data, interactive parameter sliders, diagnostics, and an explicit table of what is borrowed from the literature versus assembled here). The old Bokeh routes/templates (`/metrics_dashboard`, `/curated_metrics_dashboard`) were retired 2026-08-01.

Exercise load types (`analysis_pipeline.EXERCISE_LOAD_TYPES`) decide what a set's `weight_lb` even means, and getting it wrong is silent: bodyweight exercises (plank, leg_raise) produced all-NaN 1RM series, and assisted ones (pull_up — confirmed by a *positive* corr(weight, reps) of +0.39 where every real lift is negative) produced an **inverted** trend where rising meant weaker. Assisted exercises are converted to effective load (bodyweight − assistance) before analysis.

Production pipeline (two Lambdas, see `GARMIN_HANDOFF.md` for full detail): `garmin-data-updater` (pull, zip-packaged, daily 3:00 UTC) → `garmin-data-analyzer` (analyze + viewer-cache build, container image, daily 3:30 UTC). Only these two steps run on a schedule — activity data (running/strength/lifting) exists locally only as of 2026-07-31, not yet backfilled to S3.

## Run / validate

```bash
source .venv/bin/activate && python -m garmin.app.app          # run the app
source .venv/bin/activate && python -m pytest tests/ -q        # tests (MCMC fits deselected)
source .venv/bin/activate && python -m pytest tests/ -q -m slow # the MCMC model fits (~2 min)
source .venv/bin/activate && python -m compileall src/garmin   # syntax check
```

Full setup, env vars, and update/backfill commands: see `README.md`.

## Rules

- Keep Garmin-provider-specific logic behind `garmin/api/` — don't spread session/auth assumptions into pullers or updaters.
- Web routes read precomputed `curated/analyzed/` output only; never fit a model or hit Garmin on request.
- Don't edit generated dashboard HTML, cached data under `data/`, or notebook artifacts unless the task is explicitly about regenerating them.
- Public modules/classes/functions get docstrings; new public APIs get type hints.
- `pyproject.toml` is the packaging source of truth; `setup.py` and `requirements.txt` are compatibility shims, not where new dependencies go.

## Where to look for more

- `README.md` — setup, env vars, auth model, local/Lambda commands.
- `GARMIN_HANDOFF.md` — AWS resource names, CLI profiles, verified live state.
- `GARMIN_FIELD_AUDIT.md` — what every Garmin endpoint offers vs what we keep, and the
  **canonical source for each metric served by more than one endpoint**. Read the
  duplicate-column table before using curated data in a model: several metrics exist under
  multiple names (e.g. `sleep.avg_overnight_hrv` is byte-identical to `hrv.last_night_avg`),
  and the three ACWR-named fields are three different quantities.
- `GAME_PLAN.md` — current multi-goal roadmap (cleanup → activities data → Plotly migration → standalone deploy → predictive analysis).
- `WEB_APP_SETUP.md` — deploy spec for the future `garmin.peterwilliams.dev` standalone app (not started yet).
