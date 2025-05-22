# analysis.py: Blueprint for Garmin data analysis routes (e.g., weight plot)
import io
import base64
from flask import Blueprint, render_template, current_app
from matplotlib.figure import Figure
import pandas as pd
from garmin.db.models import HealthStats
from garmin.db.db_manager import get_db_session
from bokeh.plotting import figure
from bokeh.embed import components
from bokeh.models import DatetimeTickFormatter, RangeTool, HoverTool, ColumnDataSource, CustomJS, Slider, Select, Range1d
from bokeh.layouts import column
from bokeh.resources import CDN
import numpy as np
import math
import os
import pickle

analysis_bp = Blueprint('analysis', __name__, template_folder='templates')

# Only computation and data preparation functions should remain here.
# Move all plotting and Bokeh code to plotting.py.

# Example: keep only moving average and data wrangling utilities here.
# Remove all Bokeh plotting, layout, and JS callback code.

# (You may want to keep get_weight_plot_base64 and any other non-Bokeh, non-Flask computation here.)

def compute_moving_averages(df, col, ma_widths=None):
    import numpy as np
    if ma_widths is None:
        ma_widths = [1] + [i for i in range(7, 181, 7)]
    ma_simple = {}
    ma_gaussian = {}
    for w in ma_widths:
        ma_simple[w] = df[col].rolling(window=w, min_periods=1, center=True).mean()
        try:
            ma_gaussian[w] = df[col].rolling(window=w, win_type='gaussian', min_periods=1, center=True).mean(std=w/2)
        except Exception:
            ma_gaussian[w] = ma_simple[w]
    return ma_simple, ma_gaussian

def get_or_compute_moving_averages(df, col, ma_widths=None, cache_path=None):
    """
    Loads precomputed moving averages from a pickle file if available, otherwise computes and saves them.
    Returns (ma_simple, ma_gaussian)
    """
    if ma_widths is None:
        ma_widths = [1] + [i for i in range(7, 181, 7)]
    if cache_path is None:
        cache_path = f"/tmp/ma_cache_{col}.pkl"
    # Try to load from cache
    if os.path.exists(cache_path):
        try:
            with open(cache_path, 'rb') as f:
                cache = pickle.load(f)
            if (cache.get('ma_widths') == ma_widths and 'ma_simple' in cache and 'ma_gaussian' in cache):
                return cache['ma_simple'], cache['ma_gaussian']
        except Exception as e:
            print(f"[WARN] Could not load MA cache: {e}")
    # Compute and save
    ma_simple = {}
    ma_gaussian = {}
    for w in ma_widths:
        ma_simple[w] = df[col].rolling(window=w, min_periods=1, center=True).mean()
        try:
            ma_gaussian[w] = df[col].rolling(window=w, win_type='gaussian', min_periods=1, center=True).mean(std=w/2)
        except Exception:
            ma_gaussian[w] = ma_simple[w]
    # Save to cache
    try:
        with open(cache_path, 'wb') as f:
            pickle.dump({'ma_widths': ma_widths, 'ma_simple': ma_simple, 'ma_gaussian': ma_gaussian}, f)
    except Exception as e:
        print(f"[WARN] Could not save MA cache: {e}")
    return ma_simple, ma_gaussian
