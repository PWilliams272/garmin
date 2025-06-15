from flask import Blueprint, render_template, request
from garmin.io.db_manager import DatabaseManager
from myutils.plotting.timeseries import InteractiveTimeSeriesPlot
from bokeh.embed import components
import pandas as pd
import os
import sys

bp = Blueprint(
    'weight',
    __name__,
    template_folder="templates",
    static_folder="static"
)

@bp.route('/metrics_dashboard')
def metrics_dashboard():
    force_refresh = request.args.get('refresh', '0') == '1'

    cache_dir = os.path.join(os.path.dirname(__file__), '../../data/ma_cache')
    os.makedirs(cache_dir, exist_ok=True)

    def get_ma_cache_path(name):
        return os.path.join(cache_dir, f"{name}_ma.csv")

    def get_plot_cache_paths(name):
        return (
            os.path.join(cache_dir, f"{name}_script.html"),
            os.path.join(cache_dir, f"{name}_div.html"),
        )

    # First plot: Weight and Body Fat
    weight_script_path, weight_div_path = get_plot_cache_paths("weight_timeseries")
    print(f"[metrics_dashboard] Checking weight plot cache: {weight_script_path}, {weight_div_path}", file=sys.stderr, flush=True)
    if not force_refresh and os.path.exists(weight_script_path) and os.path.exists(weight_div_path):
        print("[metrics_dashboard] Using cached weight plot.", file=sys.stderr, flush=True)
        with open(weight_script_path) as f:
            bokeh_script_weight_timeseries = f.read()
        with open(weight_div_path) as f:
            bokeh_div_weight_timeseries = f.read()
    else:
        print("[metrics_dashboard] Generating weight plot and caching.", file=sys.stderr, flush=True)
        db_manager = DatabaseManager(environment='aws')
        df_body = db_manager.get_df('health_stats').sort_values(by='date', ascending=False)
        plot_weight = InteractiveTimeSeriesPlot(
            df_body,
            date_col='date',
            value_cols=['weight', 'body_fat'],
            y_axes=['default', 'body_fat'],
            y_axis_labels={'default': 'Weight (lb)', 'body_fat': 'Body Fat %'},
            legend_labels={'weight': 'Weight', 'body_fat': 'Body Fat'},
            show_plot=False,
            plot_height=300,
        )
        ma_path_weight = get_ma_cache_path('weight_bodyfat')
        # Always recalculate moving averages if force_refresh is set
        if not force_refresh and os.path.exists(ma_path_weight):
            print(f"[metrics_dashboard] Using cached weight MA: {ma_path_weight}", file=sys.stderr, flush=True)
            df_ma_weight = pd.read_csv(ma_path_weight, parse_dates=['date'])
        else:
            print(f"[metrics_dashboard] Generating weight MA: {ma_path_weight}", file=sys.stderr, flush=True)
            df_ma_weight = plot_weight.compute_moving_average(kernels=['gaussian', 'boxcar'], bandwidths=[1] + list(range(7, 121, 7)))
            df_ma_weight.to_csv(ma_path_weight, index=False)
        plot_weight.add_moving_average(df_ma_weight, kernel='gaussian', bandwidth=14, add_sliders=True)
        layout_weight = plot_weight.build_layout(
            add_ma_controls=True,
            add_y_sliders=True,
            add_x_slider=True,
            layout_mode='split'
        )
        bokeh_script_weight_timeseries, bokeh_div_weight_timeseries = components(layout_weight)
        with open(weight_script_path, "w") as f:
            f.write(bokeh_script_weight_timeseries)
        with open(weight_div_path, "w") as f:
            f.write(bokeh_div_weight_timeseries)

    # Second plot: Heart Rate
    hr_script_path, hr_div_path = get_plot_cache_paths("hr_timeseries")
    print(f"[metrics_dashboard] Checking HR plot cache: {hr_script_path}, {hr_div_path}", file=sys.stderr, flush=True)
    if not force_refresh and os.path.exists(hr_script_path) and os.path.exists(hr_div_path):
        print("[metrics_dashboard] Using cached HR plot.", file=sys.stderr, flush=True)
        with open(hr_script_path) as f:
            bokeh_script_hr_timeseries = f.read()
        with open(hr_div_path) as f:
            bokeh_div_hr_timeseries = f.read()
    else:
        print("[metrics_dashboard] Generating HR plot and caching.", file=sys.stderr, flush=True)
        df_hr = db_manager.get_df('heart_rate').sort_values(by='date', ascending=False)
        plot_hr = InteractiveTimeSeriesPlot(
            df_hr,
            date_col='date',
            value_cols=['resting_hr'],
            y_axes=['default'],
            y_axis_labels={'default': 'Heart Rate (bpm)'},
            legend_labels={'resting_hr': 'Resting HR'},
            show_plot=False,
            plot_height=300,
        )
        ma_path_hr = get_ma_cache_path('resting_hr')
        # Always recalculate moving averages if force_refresh is set
        if not force_refresh and os.path.exists(ma_path_hr):
            print(f"[metrics_dashboard] Using cached HR MA: {ma_path_hr}", file=sys.stderr, flush=True)
            df_ma_hr = pd.read_csv(ma_path_hr, parse_dates=['date'])
        else:
            print(f"[metrics_dashboard] Generating HR MA: {ma_path_hr}", file=sys.stderr, flush=True)
            df_ma_hr = plot_hr.compute_moving_average(kernels=['gaussian', 'boxcar'], bandwidths=[1] + list(range(7, 121, 7)))
            df_ma_hr.to_csv(ma_path_hr, index=False)
        plot_hr.add_moving_average(df_ma_hr, kernel='gaussian', bandwidth=14, add_sliders=True)
        layout_hr = plot_hr.build_layout(
            add_ma_controls=True,
            add_y_sliders=True,
            add_x_slider=True,
            layout_mode='split'
        )
        bokeh_script_hr_timeseries, bokeh_div_hr_timeseries = components(layout_hr)
        with open(hr_script_path, "w") as f:
            f.write(bokeh_script_hr_timeseries)
        with open(hr_div_path, "w") as f:
            f.write(bokeh_div_hr_timeseries)

    # Third plot: Sleep Metrics
    sleep_script_path, sleep_div_path = get_plot_cache_paths("sleep_timeseries")
    print(f"[metrics_dashboard] Checking sleep plot cache: {sleep_script_path}, {sleep_div_path}", file=sys.stderr, flush=True)
    if not force_refresh and os.path.exists(sleep_script_path) and os.path.exists(sleep_div_path):
        print("[metrics_dashboard] Using cached sleep plot.", file=sys.stderr, flush=True)
        with open(sleep_script_path) as f:
            bokeh_script_sleep_timeseries = f.read()
        with open(sleep_div_path) as f:
            bokeh_div_sleep_timeseries = f.read()
    else:
        print("[metrics_dashboard] Generating sleep plot and caching.", file=sys.stderr, flush=True)
        df_sleep = db_manager.get_df('sleep').sort_values(by='date', ascending=False)
        for col in ['deep_time', 'rem_time', 'light_time', 'awake_time', 'total_sleep_time']:
            df_sleep[col] /= 60. * 60.
        plot_sleep = InteractiveTimeSeriesPlot(
            df_sleep,
            date_col='date',
            value_cols=['deep_time', 'rem_time', 'light_time', 'awake_time', 'total_sleep_time', 'sleep_score'],
            y_axes=['default', 'default', 'default', 'default', 'default', 'sleep_score'],
            y_axis_labels={'default': 'Time (hours)', 'sleep_score': 'Sleep Score'},
            legend_labels={
                'deep_time': 'Deep Sleep',
                'rem_time': 'REM Sleep',
                'light_time': 'Light Sleep',
                'awake_time': 'Awake Time',
                'total_sleep_time': 'Total Sleep Time',
                'sleep_score': 'Sleep Score',
            },
            show_plot=False,
            plot_height=300,
        )
        ma_path_sleep = get_ma_cache_path('sleep_metrics')
        # Always recalculate moving averages if force_refresh is set
        if not force_refresh and os.path.exists(ma_path_sleep):
            print(f"[metrics_dashboard] Using cached sleep MA: {ma_path_sleep}", file=sys.stderr, flush=True)
            df_ma_sleep = pd.read_csv(ma_path_sleep, parse_dates=['date'])
        else:
            print(f"[metrics_dashboard] Generating sleep MA: {ma_path_sleep}", file=sys.stderr, flush=True)
            df_ma_sleep = plot_sleep.compute_moving_average(kernels=['gaussian', 'boxcar'], bandwidths=[1] + list(range(7, 121, 7)))
            df_ma_sleep.to_csv(ma_path_sleep, index=False)
        plot_sleep.add_moving_average(df_ma_sleep, kernel='gaussian', bandwidth=14, add_sliders=True)
        layout_sleep = plot_sleep.build_layout(
            add_ma_controls=True,
            add_y_sliders=True,
            add_x_slider=True,
            layout_mode='split'
        )
        bokeh_script_sleep_timeseries, bokeh_div_sleep_timeseries = components(layout_sleep)
        with open(sleep_script_path, "w") as f:
            f.write(bokeh_script_sleep_timeseries)
        with open(sleep_div_path, "w") as f:
            f.write(bokeh_div_sleep_timeseries)

    # Fourth plot: Steps Metrics
    steps_script_path, steps_div_path = get_plot_cache_paths("steps_timeseries")
    print(f"[metrics_dashboard] Checking steps plot cache: {steps_script_path}, {steps_div_path}", file=sys.stderr, flush=True)
    if not force_refresh and os.path.exists(steps_script_path) and os.path.exists(steps_div_path):
        print("[metrics_dashboard] Using cached steps plot.", file=sys.stderr, flush=True)
        with open(steps_script_path) as f:
            bokeh_script_steps_timeseries = f.read()
        with open(steps_div_path) as f:
            bokeh_div_steps_timeseries = f.read()
    else:
        print("[metrics_dashboard] Generating steps plot and caching.", file=sys.stderr, flush=True)
        df_steps = db_manager.get_df('steps').sort_values(by='date', ascending=False)
        df_steps_plot = df_steps.copy()
        plot_steps = InteractiveTimeSeriesPlot(
            df_steps_plot,
            date_col='date',
            value_cols=['total_steps', 'step_goal', 'total_distance'],
            y_axes=['default', 'default', 'distance'],
            y_axis_labels={'default': 'Total Steps', 'distance': 'Total Distance (m)'},
            legend_labels={
                'total_steps': 'Total Steps',
                'step_goal': 'Step Goal',
                'total_distance': 'Total Distance',
            },
            show_plot=False,
            plot_height=300,
        )
        ma_path_steps = get_ma_cache_path('steps_metrics')
        # Always recalculate moving averages if force_refresh is set
        if not force_refresh and os.path.exists(ma_path_steps):
            print(f"[metrics_dashboard] Using cached steps MA: {ma_path_steps}", file=sys.stderr, flush=True)
            df_ma_steps = pd.read_csv(ma_path_steps, parse_dates=['date'])
        else:
            print(f"[metrics_dashboard] Generating steps MA: {ma_path_steps}", file=sys.stderr, flush=True)
            df_ma_steps = plot_steps.compute_moving_average(kernels=['gaussian', 'boxcar'], bandwidths=[1] + list(range(7, 121, 7)))
            df_ma_steps.to_csv(ma_path_steps, index=False)
        plot_steps.add_moving_average(df_ma_steps, kernel='gaussian', bandwidth=14, add_sliders=True)
        layout_steps = plot_steps.build_layout(
            add_ma_controls=True,
            add_y_sliders=True,
            add_x_slider=True,
            layout_mode='split',
        )
        bokeh_script_steps_timeseries, bokeh_div_steps_timeseries = components(layout_steps)
        with open(steps_script_path, "w") as f:
            f.write(bokeh_script_steps_timeseries)
        with open(steps_div_path, "w") as f:
            f.write(bokeh_div_steps_timeseries)

    return render_template(
        'metrics_dashboard.html',
        bokeh_script_weight_timeseries=bokeh_script_weight_timeseries,
        bokeh_div_weight_timeseries=bokeh_div_weight_timeseries,
        bokeh_script_hr_timeseries=bokeh_script_hr_timeseries,
        bokeh_div_hr_timeseries=bokeh_div_hr_timeseries,
        bokeh_script_sleep_timeseries=bokeh_script_sleep_timeseries,
        bokeh_div_sleep_timeseries=bokeh_div_sleep_timeseries,
        bokeh_script_steps_timeseries=bokeh_script_steps_timeseries,
        bokeh_div_steps_timeseries=bokeh_div_steps_timeseries
    )
