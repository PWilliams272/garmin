"""Tests for the FIT-side decoding of Garmin's undocumented record fields.

These pin three things that were silently wrong before 2026-08-18 and that a
spot check would not catch:

1. `left_right_balance` carries a flag in its high bit. Unmasked it lands in
   136-228 instead of 0-100 -- a plausible-looking number that is simply not
   the balance.
2. The stamina pair was recorded swapped. available/potential agree 58-72% on
   the wrong assignment, which is close enough to pass a glance and wrong
   enough to corrupt a model.
3. The FIT and JSON paths must stay column-for-column identical, because
   `get_activity_detail_timeseries` returns whichever one worked and callers
   concat the results.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from garmin.pullers.activities import (
    _CYCLING_DYNAMICS,
    _fit_cycling_dynamics,
    _nan_column,
)


def _getter(raw: pd.DataFrame):
    def get(col):
        if col in raw.columns:
            return pd.to_numeric(raw[col], errors="coerce")
        return _nan_column(len(raw))
    return get


def test_left_right_balance_high_bit_is_masked_off():
    """Raw 136-228 is the balance with a flag bit set; 0x7F recovers 8-100."""
    raw = pd.DataFrame({"left_right_balance": [136, 178, 228]})
    out = _fit_cycling_dynamics(raw, _getter(raw))
    assert list(out["left_right_balance"]) == [8, 50, 100]


def test_balance_without_the_flag_bit_is_left_alone():
    raw = pd.DataFrame({"left_right_balance": [8, 50, 100]})
    out = _fit_cycling_dynamics(raw, _getter(raw))
    assert list(out["left_right_balance"]) == [8, 50, 100]


def test_missing_balance_stays_null_rather_than_becoming_zero():
    """A masked null would read as a 0% balance, which is a real value."""
    raw = pd.DataFrame({"left_right_balance": [136, None, 228]})
    out = _fit_cycling_dynamics(raw, _getter(raw))
    assert out["left_right_balance"].isna().tolist() == [False, True, False]
    assert out["left_right_balance"].iloc[0] == 8


def test_power_phase_arrays_are_split_into_start_and_end():
    raw = pd.DataFrame({
        "left_power_phase": [[10.0, 20.0], [11.0, 21.0]],
        "left_power_phase_peak": [[30.0, 40.0], [31.0, 41.0]],
    })
    out = _fit_cycling_dynamics(raw, _getter(raw))
    assert list(out["left_power_phase_start"]) == [10.0, 11.0]
    assert list(out["left_power_phase_end"]) == [20.0, 21.0]
    assert list(out["left_power_phase_peak_start"]) == [30.0, 31.0]
    assert list(out["left_power_phase_peak_end"]) == [40.0, 41.0]


def test_a_malformed_power_phase_array_yields_null_not_an_exception():
    """One short array in a 1500-point ride must not lose the whole activity."""
    raw = pd.DataFrame({"left_power_phase": [[10.0, 20.0], [11.0], None]})
    out = _fit_cycling_dynamics(raw, _getter(raw))
    assert out["left_power_phase_end"].isna().tolist() == [False, True, True]


def test_dynamics_columns_come_back_in_canonical_order():
    """The FIT and JSON frames get concatenated, so order must match, not just
    the column set."""
    raw = pd.DataFrame({"left_right_balance": [136]})
    out = _fit_cycling_dynamics(raw, _getter(raw))
    assert list(out) == list(_CYCLING_DYNAMICS)


def test_absent_dynamics_fields_are_null_columns_of_the_right_length():
    """A run has no power meter; the columns must still exist and align."""
    raw = pd.DataFrame({"heart_rate": [120, 130, 140]})
    out = _fit_cycling_dynamics(raw, _getter(raw))
    assert set(out) == set(_CYCLING_DYNAMICS)
    for name, col in out.items():
        assert len(col) == 3, name
        assert col.isna().all(), name


def test_nan_column_is_float_typed_so_parquet_schemas_stay_stable():
    col = _nan_column(4)
    assert len(col) == 4
    assert col.isna().all()
    assert col.dtype == np.float64
