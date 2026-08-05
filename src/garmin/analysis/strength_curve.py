"""Hierarchical Bayesian load-repetition curves for strength progression.

Replaces the "take the single best set, run it through a fixed 1RM formula"
estimate in analysis_pipeline.analyze_lifting with a model fit to the actual
sets, per exercise, over time.

Why a curve rather than a formula
---------------------------------
This account's reps sit almost entirely in the 5-10 range (p10/p50/p90 =
5/8/10). A one-rep max is therefore an *extrapolation* past the edge of the
data, and its value is driven by whichever textbook formula is chosen rather
than by anything observed. Fitting the load-rep relationship directly gives:

- a **personal** rep-decay exponent per exercise instead of a generic one,
- an estimate at a rep count actually trained (e5RM/e8RM) where the model
  interpolates and the interval is tight, with e1RM still available as a
  derived number carrying an honest, much wider interval,
- use of every set in a session (~6 here) rather than 1 of them.

The model
---------
For exercise ``e``, session ``t``, set ``i``::

    log(weight) ~ AsymmetricLaplace(mu = alpha[e, t] - beta[e] * log(reps), q)

- ``alpha[e, t]`` -- latent strength level, a Gaussian random walk over that
  exercise's session dates. Innovations scale with ``sqrt(gap_in_days)`` so
  an eight-month layoff isn't treated as one session's worth of drift.
- ``beta[e]`` -- personal rep-decay exponent, partially pooled across
  exercises. Well-identified on its own for bench press (2002 sets); the
  pooling is what keeps sparse lifts like flye (29 sessions) sane.

Why an *asymmetric* likelihood
------------------------------
This is the part that matters most, and it is not a stylistic choice. Most
sets in a session are deliberately submaximal -- warmups and back-off sets --
so the (weight, reps) cloud is dense well below true capacity. Fitting a
symmetric likelihood through the middle of that cloud is badly biased:
a plain pooled OLS of log(weight) on log(reps) for bench press returns
**beta = 0.61**, implying weight drops 4.1x going from 1 rep to 10, versus a
realistic ~1.3x. The light warmup sets drag the slope down that steeply.

An asymmetric Laplace at a high quantile (default 0.9) makes sitting *below*
the fitted line cheap and sitting above it expensive, so the curve tracks the
upper envelope of the cloud -- which is what "capacity" means -- while still
letting every set inform the shape.

Status on this account's real data
----------------------------------
Verified on synthetic data: recovers a known rep-decay exponent of 0.13 as
0.130, recovers the strength trajectory to within ~1%, converges cleanly and
is not dragged down by warmup sets. See tests/test_strength_curve.py.

**The full real fit still does not converge.** Best full-dataset run to date
is r-hat 1.22 against a 1.01 bar, so fit_strength_curves_for_all writes
nothing and the web tier falls back to the simpler top-set estimate. Do not
assume curves exist.

Measured runs, with scope, because scope turned out to matter a lot:

  11 ex  per-session nodes, unbounded beta ............ 2.88
  11 ex  + monthly knot grid (1400 -> 480 nodes) ...... 2.19
  11 ex  + beta bounded to a physiological band ....... 1.85
  11 ex  + own beta only where reps actually vary ..... 1.54
   6 ex  beta fixed outright (not estimated) .......... 2.10  <- worse
   2 ex  + centered walk, tune 2500, accept 0.95 ...... 1.01  <- converged
  11 ex  same settings, full dataset .................. 1.22  <- best so far
   1 ex  bench press fitted alone ..................... 1.83  <- worse

Two negative results carry most of the information. Fixing beta made things
worse, ruling it out as the cause. Fitting a single exercise alone was worse
than the joint model, ruling out "the joint problem is simply too large" and
showing the cross-exercise pooling is load-bearing.

Note the non-monotonicity: 1.83 at one exercise, 1.01 at two, 1.22 at eleven.
That pattern is not a scaling wall; it looks like a multi-modal posterior
where whether chains find the same mode is partly luck.

That suspicion has now been tested, and it was wrong. The likelihood was the
leading suspect -- AsymmetricLaplace is a *pseudo*-likelihood for quantile
regression rather than a generative model, and its geometry is known to be
awkward -- so it was replaced with an explicit stochastic-frontier account of
submaximal effort (capacity minus a one-sided shortfall; Aigner, Lovell &
Schmidt 1977) and both were run on the same data with the same settings:

  14 ex  AsymmetricLaplace ...... r-hat 1.22,  0 divergences, 17.6 min
  14 ex  stochastic frontier .... r-hat 2.28, 11 divergences, 45.2 min

The generative model is *worse* on every axis, and by a wide margin. It is
better motivated -- it says how a set is produced, and its effort parameters
mean something -- but it samples badly here: it adds four parameters coupled
to the level, and its likelihood stiffens sharply near the frontier where the
best sets sit, which is exactly where the level wants to move. On synthetic
data drawn from the frontier process itself it recovers beta (0.126 against a
true 0.13) and the capacity path to ~2%, so the implementation is sound; the
problem is the posterior geometry on real data.

So the default stays LIKELIHOOD_ASYMMETRIC, and the cause of the 1.22 is
still unidentified. Both remain selectable via `likelihood=`. What this rules
out is worth as much as a fix would have been: three of the four candidate
causes (beta, problem size, likelihood form) are now eliminated, leaving the
random-walk level's own geometry as the remaining suspect -- a non-centered
parameterisation of the *innovations* specifically, or a coarser knot grid,
is where to look next.

The convergence gate in fit_strength_curves_for_all stays regardless: callers
must check `diagnostics["converged"]` before persisting anything, because a
non-converged posterior still produces confident-looking intervals.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

# Rep counts to report a max-ability estimate at. 5 and 8 are inside this
# account's trained range (interpolation, tight intervals); 1 is the familiar
# number but is extrapolated and will carry a visibly wider band.
DEFAULT_REP_TARGETS: tuple[int, ...] = (1, 5, 8)

# Quantile the fitted curve tracks. Only used by LIKELIHOOD_ASYMMETRIC; 0.9
# keeps the curve near the top of each session's set cloud without chasing a
# single fluke rep.
DEFAULT_QUANTILE = 0.9

# How submaximal sets are accounted for.
#
# FRONTIER models it generatively: capacity, minus a one-sided shortfall whose
# size depends on why the set was easy. ASYMMETRIC is the earlier quantile
# pseudo-likelihood, kept so the change can be measured rather than asserted.
LIKELIHOOD_FRONTIER = "frontier"
LIKELIHOOD_ASYMMETRIC = "asymmetric_laplace"

# Physiological bounds on the rep-decay exponent, which beta is squashed into.
#
# beta is what fraction of a 1RM survives at higher reps: a 10RM sits at
# 10**-beta of the one-rep max, so beta = 0.15 gives ~71%, matching standard
# load-rep tables (10RM ~= 70-75%). beta = 0.35 already implies 45%, and
# anything past that is not a thing bodies do.
#
# Bounding this is what makes the model identifiable, and it is not a
# convenience hack. Rep variation in this account is very low -- `row` does
# 69% of its sets at exactly 10 reps, `curl` and `triceps_extension` 66-67%
# -- so for those exercises the data carries almost no information about the
# *slope* of the load-rep curve, and an unbounded beta wanders off to 0.4-0.6
# (r-hat 2.2, physiologically impossible) while trading off against the
# strength level. beta genuinely doesn't vary much across lifts, so encoding
# that is exactly what the hierarchical prior is for.
BETA_MIN = 0.03
BETA_MAX = 0.35
# Centre of the logit-scale prior: sigmoid(-0.7) ~= 0.33 maps to beta ~= 0.14,
# the Epley-equivalent slope around 8 reps.
BETA_LOGIT_MU = -0.7
BETA_LOGIT_SIGMA = 0.7

# How far individual exercises may drift from the shared rep-decay exponent,
# on the logit scale. Deliberately tight (~+/-0.02 in beta terms).
#
# When reps barely vary, alpha and beta are not separately identified at all:
# every set at 10 reps constrains only `alpha - beta*log(10)`, so any increase
# in beta buys an equal increase in the strength level. That's a ridge in the
# posterior rather than a peak, and chains slide along it -- which is what
# produced r-hat 2.2 with a loose prior, and beta pinned against its ceiling
# with a bounded one. Strong partial pooling is the honest resolution: where
# the data can't speak to an exercise's own slope, it borrows the population
# value, while a well-sampled lift like bench press (2005 sets, rep sd 1.9)
# still has enough evidence to pull away from it.
BETA_GROUP_SIGMA = 0.25

# An exercise needs at least this much spread in its rep counts before it gets
# to estimate its *own* rep-decay exponent; below it, beta is pinned to the
# shared group value.
#
# This is the direct fix for the alpha/beta ridge described above. Softening
# the prior wasn't enough: with most exercises uninformative, they collectively
# dragged the group mean to an impossible ~0.32 (10RM at 48% of 1RM) and
# outvoted the one lift that *did* have evidence. Removing their free
# parameter entirely means the group exponent is estimated only from lifts
# with real rep variation -- measured here: bench_press 1.88, deadlift 1.74,
# squat 1.67, shoulder_press 1.55 clear this; curl 1.11, triceps_extension
# 1.12 and row 0.98 (69% of its sets at exactly 10 reps) do not.
REP_SD_FOR_OWN_BETA = 1.5

# An exercise needs at least this many sessions to contribute its own curve.
MIN_SESSIONS_FOR_CURVE = 8

# Spacing of the latent strength grid, in days. The level is a random walk
# over these knots and each session reads a linear interpolation between the
# two surrounding ones, rather than every session carrying its own free
# parameter.
#
# This is both a practical and a modelling improvement. One node per session
# meant ~1,500 latent parameters across 12 exercises, which took 20+ minutes
# and still came back at r-hat 2.9 (chains nowhere near mixed). It also let
# the level jump between consecutive same-week sessions, which isn't how
# strength behaves -- real change happens over weeks. Monthly knots cut the
# parameter count roughly tenfold and impose that smoothness directly.
KNOT_SPACING_DAYS = 30.0

# Standard Gelman-Rubin convergence bar. Above this the chains haven't mixed
# and the posterior summaries are meaningless, however plausible they look.
RHAT_THRESHOLD = 1.01


# Set position within an exercise, past which effort starts falling away
# again. Measured on this account: mean shortfall below the session's best is
# 0.234 log units at position 1 (warmup), bottoms out at 0.035-0.056 across
# positions 2-4, then climbs back to 0.246 by position 7+. The rise is what
# this knot marks.
FATIGUE_ONSET_POSITION = 4


EFFORT_TERMS = ("warmup", "fatigue")


def _effort_covariates(position: np.ndarray) -> np.ndarray:
    """Design matrix for how far below capacity a set is likely to sit.

    Deliberately not a linear term in set position. Measured against the
    session's own best effort, shortfall is U-shaped -- high at position 1,
    near zero through the working sets, rising again late -- so a straight
    line through it finds essentially nothing (r = -0.03 on 6,060 sets). The
    two terms here match the shape that is actually there:

    - `warmup`  -- an indicator for the opening set, which carries 1.6x to
      6.6x the shortfall of the rest depending on the lift (biggest on the
      heavy compounds that need warming up, smallest on isolation work).
    - `fatigue` -- how far past FATIGUE_ONSET_POSITION the set is, capturing
      the late-session climb back up.
    """
    warmup = (position <= 1).astype(float)
    fatigue = np.maximum(0.0, position - FATIGUE_ONSET_POSITION)
    return np.column_stack([warmup, fatigue])


def _prepare_exercise_sets(sets: pd.DataFrame) -> pd.DataFrame:
    """Drop rows the model can't use, normalise dtypes, and derive the
    within-session set position the effort model needs.

    Only positive weights and reps are usable: a log-log curve is undefined
    at zero, and zero-weight rows mean the exercise is bodyweight and should
    never have reached this module (see analysis_pipeline.EXERCISE_LOAD_TYPES).

    Sets flagged `duration_suspect` (the watch was left running, so the set
    looks minutes long) are kept -- the flag is about timing, and weight and
    reps are still good -- but their tempo is not trusted.
    """
    columns = ["date", "reps", "weight_lb"]
    optional = [c for c in ("activity_id", "set_number") if c in sets.columns]
    df = sets[columns + optional].copy()
    df["date"] = pd.to_datetime(df["date"])
    df["reps"] = pd.to_numeric(df["reps"], errors="coerce")
    df["weight_lb"] = pd.to_numeric(df["weight_lb"], errors="coerce")
    df = df.dropna(subset=["date", "reps", "weight_lb"])
    df = df[(df["reps"] > 0) & (df["weight_lb"] > 0)]

    # Position *within this exercise in this session*, which is what "first
    # working set" means. Garmin's own set_number counts across the whole
    # activity, so in a bench/curl/bench superset the first curl set carries a
    # high set_number -- using it directly would label warmups as late sets.
    if "activity_id" in df.columns:
        order = "set_number" if "set_number" in df.columns else "date"
        df = df.sort_values(["activity_id", order])
        df["set_position"] = df.groupby("activity_id").cumcount() + 1
    else:
        df = df.sort_values("date")
        df["set_position"] = df.groupby("date").cumcount() + 1

    return df.sort_values("date").reset_index(drop=True)


def _knot_interpolation(
    dates: pd.DatetimeIndex, origin: pd.Timestamp, n_knots: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Map dates onto the knot grid: (lower index, upper index, weight).

    A date sitting exactly on a knot gets weight 0 and both indices equal, so
    the interpolation degenerates cleanly to that knot's value.
    """
    elapsed = (dates.to_numpy() - origin.to_numpy()).astype("timedelta64[D]").astype(float)
    position = np.clip(elapsed / KNOT_SPACING_DAYS, 0.0, n_knots - 1.0)
    lower = np.floor(position).astype(int)
    lower = np.clip(lower, 0, n_knots - 1)
    upper = np.clip(lower + 1, 0, n_knots - 1)
    return lower, upper, position - lower


def _build_design(
    sets_by_exercise: dict[str, pd.DataFrame], min_sessions: int
) -> tuple[dict, list[str]]:
    """Flatten per-exercise sets into the index arrays the model needs.

    Each exercise gets its own contiguous block of *knots* (see
    KNOT_SPACING_DAYS); the block offsets let one flat `alpha` vector carry
    every exercise's random walk. Every set and every session date is stored
    as a pair of knot indices plus an interpolation weight.
    """
    exercises: list[str] = []
    session_dates: list[pd.DatetimeIndex] = []
    knot_counts: list[int] = []
    rep_sds: list[float] = []
    offsets: list[int] = []
    session_interp: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    log_w: list[np.ndarray] = []
    log_r: list[np.ndarray] = []
    effort_x: list[np.ndarray] = []
    obs_lower: list[np.ndarray] = []
    obs_upper: list[np.ndarray] = []
    obs_weight: list[np.ndarray] = []
    ex_idx: list[np.ndarray] = []

    offset = 0
    for exercise, raw in sorted(sets_by_exercise.items()):
        df = _prepare_exercise_sets(raw)
        if df.empty:
            continue
        dates = pd.DatetimeIndex(sorted(df["date"].unique()))
        if len(dates) < min_sessions:
            continue

        origin = dates.min()
        span_days = float((dates.max() - origin).days)
        n_knots = max(2, int(np.ceil(span_days / KNOT_SPACING_DAYS)) + 1)

        exercises.append(exercise)
        session_dates.append(dates)
        knot_counts.append(n_knots)
        offsets.append(offset)
        session_interp.append(_knot_interpolation(dates, origin, n_knots))

        rep_sds.append(float(df["reps"].std()))
        set_dates = pd.DatetimeIndex(df["date"])
        lower, upper, weight = _knot_interpolation(set_dates, origin, n_knots)
        log_w.append(np.log(df["weight_lb"].to_numpy(dtype=float)))
        log_r.append(np.log(df["reps"].to_numpy(dtype=float)))
        effort_x.append(_effort_covariates(df["set_position"].to_numpy(dtype=float)))
        obs_lower.append(lower + offset)
        obs_upper.append(upper + offset)
        obs_weight.append(weight)
        ex_idx.append(np.full(len(df), len(exercises) - 1))
        offset += n_knots

    if not exercises:
        return {}, []

    return {
        "log_w": np.concatenate(log_w),
        "log_r": np.concatenate(log_r),
        "effort_x": np.concatenate(effort_x, axis=0),
        "obs_lower": np.concatenate(obs_lower).astype(int),
        "obs_upper": np.concatenate(obs_upper).astype(int),
        "obs_weight": np.concatenate(obs_weight),
        "ex_idx": np.concatenate(ex_idx).astype(int),
        "session_dates": session_dates,
        "session_interp": session_interp,
        "knot_counts": knot_counts,
        "rep_sds": np.array(rep_sds),
        "offsets": offsets,
        "n_alpha": offset,
    }, exercises


# Prior scale for the effort-model coefficients, on the log scale. 0.7 lets a
# term multiply the mean shortfall by roughly 2x either way inside one sd --
# wide enough for the 6.6x warmup effect measured on bench press to be reached,
# without inviting the sampler to explore absurd values.
EFFORT_COEF_SIGMA = 0.7

# Prior mean for log(mean shortfall). exp(-3.0) ~= 0.05 log units, matching the
# measured median shortfall of a working set (0.026) to within a factor of two.
EFFORT_INTERCEPT_MU = -3.0
EFFORT_INTERCEPT_SIGMA = 1.0


# Below this z, log(erfc(-z/sqrt(2))) loses its precision to underflow and the
# asymptotic tail expansion takes over instead. erfc underflows in float64
# somewhere past an argument of ~26; -6 is comfortably inside that, and the two
# branches agree to ~1e-3 in log density where they meet.
_LOGPHI_TAIL_Z = -6.0
_LOG_SQRT_2PI = 0.9189385332046727


def _log_normal_cdf(pt, z):
    """log(Phi(z)), numerically safe and JAX-compilable.

    PyMC's own ``logcdf`` for a Normal is built on ``erfcx``, which pytensor
    cannot lower to JAX -- so sampling this model with the numpyro backend
    fails outright unless the log-CDF is spelled out. ``erfc`` *does* have a
    JAX kernel, so the central branch uses it directly; far into the left tail
    it underflows to zero and log would return -inf, so that region falls back
    to the standard asymptotic expansion::

        log Phi(z) ~= -z^2/2 - log(-z) - log(sqrt(2*pi))       (z << 0)

    Both branches are always evaluated (pt.switch is not lazy), so the tail
    input is clamped away from zero to keep the unused branch finite -- an
    inf or nan in the discarded side still poisons the gradient.
    """
    safe_z = pt.minimum(z, _LOGPHI_TAIL_Z)
    # Two correction terms of the asymptotic series, not just the leading one:
    # at the switch point the 1/z^2 term is worth 0.026 in log density, which
    # is precisely the discontinuity the leading term alone leaves behind.
    inv_sq = 1.0 / safe_z ** 2
    tail = (
        -0.5 * safe_z ** 2
        - pt.log(-safe_z)
        - _LOG_SQRT_2PI
        + pt.log1p(-inv_sq + 3.0 * inv_sq ** 2)
    )
    central = pt.log(pt.erfc(-pt.maximum(z, _LOGPHI_TAIL_Z) / np.sqrt(2.0)) / 2.0)
    return pt.switch(z < _LOGPHI_TAIL_Z, tail, central)


def _frontier_logp(pm, pt, residual, sigma_u, sigma_v):
    """Log density of a normal-exponential composed error.

    This is the stochastic frontier likelihood (Aigner, Lovell & Schmidt 1977;
    Meeusen & van den Broeck 1977), borrowed from production economics where
    it models output sitting below an efficient frontier. The mapping here is
    direct: the frontier is capacity, and the one-sided term is how far below
    capacity a set was actually taken.

    ``residual = log(weight) - capacity`` is decomposed as ``v - u`` with
    ``v ~ Normal(0, sigma_v)`` measurement noise and ``u ~ Exponential(mean =
    sigma_u)`` the effort shortfall. Marginalising out ``u`` analytically is
    what makes this affordable -- carrying one latent ``u`` per set would add
    ~9,000 parameters and is exactly the kind of geometry that wrecked earlier
    attempts::

        f(e) = 1/su * Phi(-e/sv - sv/su) * exp(e/su + sv^2/(2 su^2))

    Unlike the AsymmetricLaplace it replaces, this is a genuine generative
    model: it states how a set is produced, so the shortfall is a quantity
    with a meaning rather than a tuning knob.
    """
    z = -residual / sigma_v - sigma_v / sigma_u
    return (
        -pt.log(sigma_u)
        + residual / sigma_u
        + (sigma_v ** 2) / (2.0 * sigma_u ** 2)
        + _log_normal_cdf(pt, z)
    )


def _hierarchical_beta(pm, pt, design: dict, n_ex: int):
    """Partially-pooled rep-decay exponent (see BETA_* constants)."""
    mu_beta = pm.Normal("mu_beta", BETA_LOGIT_MU, BETA_LOGIT_SIGMA)
    # A hierarchical spread needs several groups to be identified at all;
    # with one exercise it's a pure funnel and wrecks convergence. Fall
    # back to complete pooling in that case rather than sampling garbage.
    # Only exercises whose reps actually vary get their own deviation;
    # the rest are pinned to the shared value (see REP_SD_FOR_OWN_BETA).
    free_beta = design["rep_sds"] >= REP_SD_FOR_OWN_BETA
    n_free = int(free_beta.sum())
    if n_ex > 1 and n_free > 1:
        sigma_beta = pm.HalfNormal("sigma_beta", BETA_GROUP_SIGMA)
        offsets_free = pm.Normal("beta_offset", 0.0, 1.0, shape=n_free)
        deviation = pt.zeros(n_ex)
        deviation = pt.set_subtensor(
            deviation[np.flatnonzero(free_beta)], sigma_beta * offsets_free
        )
        beta_logit = mu_beta + deviation
    else:
        beta_logit = mu_beta * pm.math.ones(n_ex)
    # Squash into the physiological band (see BETA_MIN/BETA_MAX). A smooth
    # sigmoid rather than a clamp: hard bounds put a kink in the posterior
    # that NUTS handles badly, and the logit scale also gives the
    # hierarchy sane geometry.
    beta = pm.Deterministic(
        "beta", BETA_MIN + (BETA_MAX - BETA_MIN) * pm.math.sigmoid(beta_logit)
    )
    return beta


def fit_strength_curves(
    sets_by_exercise: dict[str, pd.DataFrame],
    *,
    quantile: float = DEFAULT_QUANTILE,
    rep_targets: Sequence[int] = DEFAULT_REP_TARGETS,
    min_sessions: int = MIN_SESSIONS_FOR_CURVE,
    draws: int = 1500,
    # Long warmup and a high acceptance target are both load-bearing, not
    # caution: at tune=800/target_accept=0.9 this model sits around r-hat 1.2,
    # and only reaches the 1.01 bar once the sampler is given enough warmup to
    # adapt to the random walk's correlated geometry.
    tune: int = 2500,
    chains: int = 2,
    target_accept: float = 0.95,
    random_seed: int = 20260803,
    progressbar: bool = False,
    nuts_sampler: str = "numpyro",
    fixed_beta: float | None = None,
    likelihood: str = LIKELIHOOD_ASYMMETRIC,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Fit every exercise's load-rep curve jointly and return per-date eXRM.

    `sets_by_exercise` maps exercise name -> a frame with `date`, `reps` and
    `weight_lb` columns (one row per set). Exercises with fewer than
    `min_sessions` distinct session dates are skipped rather than fit on
    noise.

    Returns ``(curves, betas, diagnostics)``:
    - `curves` -- exercise -> frame of one row per session date with, for
      each rep target X, `e{X}rm_mean` plus 68% and 95% credible bounds.
    - `betas` -- one row per exercise with the posterior rep-decay exponent.
    - `diagnostics` -- `max_rhat`, `divergences`, `converged`. **Check
      `converged` before persisting or displaying anything**: the summaries
      are computed regardless, and a non-converged fit looks entirely
      plausible until you check.

    `likelihood` selects how submaximal sets are handled: LIKELIHOOD_FRONTIER
    (default) models the shortfall generatively, LIKELIHOOD_ASYMMETRIC keeps
    the earlier quantile-regression pseudo-likelihood so the two can be
    compared on the same data.

    Importing PyMC is deferred to call time: it's a heavy import, and the
    daily analyzer Lambda imports this module's package without ever fitting.
    """
    if likelihood not in (LIKELIHOOD_FRONTIER, LIKELIHOOD_ASYMMETRIC):
        raise ValueError(
            f"unknown likelihood {likelihood!r}; "
            f"expected {LIKELIHOOD_FRONTIER!r} or {LIKELIHOOD_ASYMMETRIC!r}"
        )
    import pymc as pm
    import pytensor.tensor as pt

    design, exercises = _build_design(sets_by_exercise, min_sessions)
    if not exercises:
        empty_betas = pd.DataFrame(columns=["exercise", "beta_mean", "beta_lower_95", "beta_upper_95"])
        return {}, empty_betas, {"max_rhat": float("nan"), "divergences": 0, "converged": False}

    n_ex = len(exercises)

    with pm.Model() as model:
        if fixed_beta is not None:
            # Treat the rep-decay exponent as known rather than estimated.
            # This dissolves the alpha/beta ridge outright: with beta given,
            # every set at any rep count speaks directly to the strength
            # level. The cost is that beta is no longer personal -- but when
            # reps barely vary, a "personal" beta is unidentifiable anyway,
            # so estimating one is false precision rather than extra insight.
            beta = pm.Deterministic("beta", pt.as_tensor_variable(
                np.full(n_ex, float(fixed_beta))
            ))
        else:
            beta = _hierarchical_beta(pm, pt, design, n_ex)

        # Knots are evenly spaced, so one scale covers every step -- it reads
        # directly as "log-strength drift per KNOT_SPACING_DAYS".
        sigma_walk = pm.HalfNormal("sigma_walk", 0.05)

        # Centered random walk, deliberately. The usual advice is to
        # non-center hierarchical terms, and this model originally did -- but
        # non-centering helps when a parameter is weakly informed by data, and
        # here it's the opposite: ~34 observations sit near every knot. In
        # that strongly-informed regime the centered form has much better
        # geometry. Measured on real data, swapping to it (together with the
        # longer warmup above) is what took the innovations from r-hat 1.25 to
        # the 1.01 bar.
        alpha_blocks = [
            pm.GaussianRandomWalk(
                f"alpha_{e}",
                sigma=sigma_walk,
                init_dist=pm.Normal.dist(np.log(100.0), 2.0),
                shape=design["knot_counts"][e],
            )
            for e in range(n_ex)
        ]
        alpha = pm.Deterministic("alpha", pm.math.concatenate(alpha_blocks))

        # Linear interpolation between the two surrounding knots.
        w = design["obs_weight"]
        level = (1.0 - w) * alpha[design["obs_lower"]] + w * alpha[design["obs_upper"]]
        # The capacity frontier: what this exercise could lift at this rep
        # count on this date, if the set were taken to the limit.
        mu = level - beta[design["ex_idx"]] * design["log_r"]

        if likelihood == LIKELIHOOD_FRONTIER:
            # How far below capacity a set sits is modelled, not assumed. The
            # mean shortfall varies with why the set was submaximal -- warming
            # up, or fading late in the session.
            effort_x = design["effort_x"]
            g0 = pm.Normal("effort_intercept", EFFORT_INTERCEPT_MU, EFFORT_INTERCEPT_SIGMA)
            g = pm.Normal("effort_coef", 0.0, EFFORT_COEF_SIGMA, shape=effort_x.shape[1])
            sigma_u = pm.Deterministic(
                "sigma_u", pt.exp(g0 + pt.dot(pt.as_tensor_variable(effort_x), g))
            )
            sigma_v = pm.HalfNormal("sigma_v", 0.1)
            pm.Potential(
                "obs", _frontier_logp(pm, pt, design["log_w"] - mu, sigma_u, sigma_v)
            )
        else:
            b = pm.HalfNormal("b", 1.0)
            pm.AsymmetricLaplace("obs", mu=mu, b=b, q=quantile, observed=design["log_w"])

        # The full real dataset (~9k sets across ~1.5k session nodes) takes
        # 20+ minutes on PyMC's default C-backend NUTS, which is impractical
        # even for a weekly offline job; the JAX-backed numpyro sampler cuts
        # that dramatically. Fall back gracefully if it isn't installed.
        sample_kwargs = dict(
            draws=draws, tune=tune, chains=chains, target_accept=target_accept,
            random_seed=random_seed, progressbar=progressbar,
        )
        try:
            idata = pm.sample(nuts_sampler=nuts_sampler, **sample_kwargs)
        except (ImportError, ModuleNotFoundError, ValueError) as exc:
            if nuts_sampler == "pymc":
                raise
            print(f"[strength_curve] {nuts_sampler} sampler unavailable ({exc}); using default NUTS.")
            idata = pm.sample(**sample_kwargs)

    diagnostics = _report_diagnostics(idata)
    curves, betas = _summarize(idata, design, exercises, rep_targets)
    return curves, betas, diagnostics


def _report_diagnostics(idata) -> dict:
    """Worst r-hat and divergence count, printed and returned.

    A silently non-converged fit is worse than no fit at all -- it still
    produces confident-looking intervals that read as real. Observed live on
    the full dataset: max r-hat 2.88 yielded a lateral-raise beta of 0.41,
    implying a 2.4x jump from an 8-rep to a 1-rep max where ~1.3x is
    realistic. Callers are expected to check `converged` before persisting
    anything (see analysis_pipeline.fit_strength_curves_for_all).
    """
    import arviz as az

    # Which variables exist depends on how beta was handled (mu_beta and
    # sigma_beta are absent entirely when it's fixed), so check what's there.
    candidates = ["beta", "mu_beta", "sigma_beta", "sigma_walk", "alpha", "b"]
    present = [v for v in candidates if v in idata.posterior]
    summary = az.summary(idata, var_names=present, round_to=4)
    max_rhat = float(summary["r_hat"].max())
    divergences = int(idata.sample_stats["diverging"].sum())
    converged = max_rhat <= RHAT_THRESHOLD and divergences == 0
    print(
        f"[strength_curve] {'OK' if converged else 'NOT CONVERGED'}: "
        f"max r-hat={max_rhat:.4f}, divergences={divergences}"
    )
    return {"max_rhat": max_rhat, "divergences": divergences, "converged": converged}


def _summarize(
    idata, design: dict, exercises: list[str], rep_targets: Sequence[int]
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Turn posterior draws into per-date eXRM frames and a beta table."""
    posterior = idata.posterior
    # (chain, draw, node) -> (sample, node)
    alpha = posterior["alpha"].stack(sample=("chain", "draw")).transpose("sample", ...).to_numpy()
    beta = posterior["beta"].stack(sample=("chain", "draw")).transpose("sample", ...).to_numpy()

    curves: dict[str, pd.DataFrame] = {}
    beta_rows = []
    for e, exercise in enumerate(exercises):
        offset = design["offsets"][e]
        dates = design["session_dates"][e]
        lower, upper, weight = design["session_interp"][e]
        # Read the latent level at each session date off the knot grid, the
        # same interpolation the likelihood used.
        block = (
            (1.0 - weight) * alpha[:, lower + offset] + weight * alpha[:, upper + offset]
        )

        frame = pd.DataFrame({"date": dates})
        for target in rep_targets:
            # eXRM = exp(alpha - beta * log(X)); X=1 makes log(X)=0, so the
            # 1RM column is just exp(alpha) -- the extrapolated intercept.
            draws_x = np.exp(block - beta[:, [e]] * np.log(float(target)))
            frame[f"e{target}rm_mean"] = draws_x.mean(axis=0)
            for label, (lo, hi) in {"68": (16, 84), "95": (2.5, 97.5)}.items():
                frame[f"e{target}rm_lower_{label}"] = np.percentile(draws_x, lo, axis=0)
                frame[f"e{target}rm_upper_{label}"] = np.percentile(draws_x, hi, axis=0)
        curves[exercise] = frame

        beta_rows.append({
            "exercise": exercise,
            "beta_mean": float(beta[:, e].mean()),
            "beta_lower_95": float(np.percentile(beta[:, e], 2.5)),
            "beta_upper_95": float(np.percentile(beta[:, e], 97.5)),
        })

    return curves, pd.DataFrame(beta_rows)
