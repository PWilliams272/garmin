"""Deciding which sessions are evidence about maximum strength.

An estimated 1RM assumes at least one set in the session was taken near
failure. That assumption is what makes `max(e1RM)` across a session's sets a
measurement of *capacity* rather than of effort, and it is not always true:

- **Abandoned sessions.** One warmup set, then a change of mind. The session's
  "best" set is a warmup, and reading it as a capacity estimate drags the
  trend down at exactly that date.
- **Easy days.** A deliberately light session, a deload, a day when nothing
  felt good. Every set is real training, but none of them probed the ceiling.

Both are the same statistical situation: the session tells you capacity was
*at least* this much, not that it *was* this much. A left-censored observation
being treated as a point observation.

The asymmetry is the whole basis for detecting them
---------------------------------------------------
Error here only runs one way. You cannot accidentally lift more than you are
capable of, but you can very easily lift much less. So a session sitting far
*below* its neighbours is suspect while one sitting above is simply a good
day, and rejection has to be one-sided. A symmetric outlier rule would throw
away exactly the maximal efforts that carry the most information.

Measured on this account, deviation from the local median is tight in the
middle and has a heavy lower tail -- p25 to p75 spans only -0.029 to +0.031
in log units (about +/-3%), then breaks to -0.203 at p5 and -0.583 at p2.
That break is a separate population, not the edge of a continuum, which is
what makes a threshold defensible rather than arbitrary.

Why a *local* level, and why that matters
-----------------------------------------
Sessions are compared against a centred rolling median, not a global one.
This is deliberate and load-bearing: a genuine decline -- detraining, injury,
time off -- moves the local level with it, so only *isolated* dips look
anomalous. Comparing against a global level would let this quietly delete
every real setback and manufacture a trend that only ever rises.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# A session needs at least this many usable sets to speak to capacity.
#
# Session best rises monotonically with set count on this account -- 1 set
# reaches 80% of the median session best, 2 reaches 90%, 3 reaches 94%, 5
# reaches 98%, 6+ reaches 104%. A single-set session is the abandoned case
# almost by definition: a warmup and then a change of plan.
MIN_SETS_FOR_CAPACITY = 2

# How far below the local level a session may sit before it stops counting as
# evidence of capacity. 0.30 in log units is 26% down.
#
# Sits in the gap between the bulk of sessions (p5 is -0.203) and the clearly
# anomalous tail (p2 is -0.583), and rejects about 3% of sessions. Loose
# enough that ordinary variation survives, tight enough to catch a session
# whose top set was a warmup.
MAX_SHORTFALL_LOG = 0.30

# Window for the local level. Long enough to be robust to a few bad sessions,
# short enough to follow a real change in strength rather than averaging over
# it.
LEVEL_WINDOW = "120D"
MIN_SESSIONS_FOR_LEVEL = 5


def flag_submaximal_sessions(
    sessions: pd.DataFrame,
    *,
    value_column: str = "est_1rm",
    group_columns: tuple[str, ...] = ("family",),
    min_sets: int = MIN_SETS_FOR_CAPACITY,
    max_shortfall: float = MAX_SHORTFALL_LOG,
    window: str = LEVEL_WINDOW,
) -> pd.DataFrame:
    """Mark sessions that don't support a maximum-strength reading.

    `sessions` needs a `date`, the value column, and (optionally) `n_sets`.
    Adds three columns:

    - `local_level` -- the centred rolling median the session is judged against
    - `shortfall`   -- log(local_level / value), so positive means "below the
      local level"; 0 where no level could be computed
    - `submaximal`  -- True when the session should not be read as capacity

    Nothing is dropped here. Callers decide what to do with the flag, because
    the right answer differs: a trend fit should exclude these, while a
    training-volume count should still include them -- an easy day is still a
    day you trained.
    """
    out = sessions.copy()
    if out.empty:
        for column in ("local_level", "shortfall", "submaximal"):
            out[column] = pd.Series(dtype="float64" if column != "submaximal" else "bool")
        return out

    out["local_level"] = np.nan
    for _key, group in out.groupby(list(group_columns), sort=False):
        ordered = group.sort_values("date")
        level = (
            ordered.set_index("date")[value_column]
            .rolling(window, center=True, min_periods=MIN_SESSIONS_FOR_LEVEL)
            .median()
        )
        out.loc[ordered.index, "local_level"] = level.to_numpy()

    with np.errstate(all="ignore"):
        shortfall = np.log(out["local_level"] / out[value_column])
    # A session with no computable level is not evidence of anything wrong.
    out["shortfall"] = shortfall.replace([np.inf, -np.inf], np.nan).fillna(0.0)

    too_few_sets = (
        out["n_sets"] < min_sets if "n_sets" in out.columns
        else pd.Series(False, index=out.index)
    )
    out["submaximal"] = (out["shortfall"] > max_shortfall) | too_few_sets
    return out


def capacity_sessions(sessions: pd.DataFrame, **kwargs) -> pd.DataFrame:
    """Just the sessions that can be read as capacity observations.

    Convenience for the common case. Keeps every session when the filter would
    leave too little to fit anything, since a sparsely-trained exercise is
    better served by noisy data than by no data.
    """
    flagged = flag_submaximal_sessions(sessions, **kwargs)
    kept = flagged[~flagged["submaximal"]]
    if len(kept) < MIN_SESSIONS_FOR_LEVEL:
        return flagged
    return kept
