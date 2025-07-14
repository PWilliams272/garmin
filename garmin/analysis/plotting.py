"""
plotting.py: Bokeh plotting utilities for Garmin metrics dashboard
"""

from bokeh.embed import components
from myutils.plotting.timeseries import InteractiveTimeSeriesPlot

def trim_moving_average_range(df, min, max):
    df = df.copy()
    columns = df.columns.tolist()
    col_list = []
    for col in columns:
        if col == 'date':
            col_list.append(col)
            continue
        bw = int(col.split('_')[-1])
        if bw < min or bw > max:
            continue
        col_list.append(col)
    return df[col_list]

def make_health_stats_bokeh_plot(df, df_ma, ma_lims=(1, 150)):
    df_ma = df_ma.copy()
    df_ma = trim_moving_average_range(df_ma, ma_lims[0], ma_lims[1])
    plot = InteractiveTimeSeriesPlot(
        df,
        date_col='date',
        value_cols=['weight', 'body_fat'],
        y_axes=['default', 'body_fat'],
        y_axis_labels={'default': 'Weight (lb)', 'body_fat': 'Body Fat %'},
        legend_labels={'weight': 'Weight', 'body_fat': 'Body Fat'},
        show_plot=False,
        plot_height=300,
    )
    plot.add_moving_average(df_ma, kernel='gaussian', bandwidth=14, add_sliders=True)
    layout = plot.build_layout(
        add_ma_controls=True,
        add_y_sliders=True,
        add_x_slider=True,
        layout_mode='split'
    )
    script, div = components(layout)
    return script, div

def makeheart_rate_bokeh_plot(df, df_ma, ma_lims=(1, 150)):
    df_ma = df_ma.copy()
    df_ma = trim_moving_average_range(df_ma, ma_lims[0], ma_lims[1])
    plot = InteractiveTimeSeriesPlot(
        df,
        date_col='date',
        value_cols=['resting_hr'],
        y_axes=['default'],
        y_axis_labels={'default': 'Heart Rate (bpm)'},
        legend_labels={'resting_hr': 'Resting HR'},
        show_plot=False,
        plot_height=300,
    )
    plot.add_moving_average(df_ma, kernel='gaussian', bandwidth=14, add_sliders=True)
    layout = plot.build_layout(
        add_ma_controls=True,
        add_y_sliders=True,
        add_x_slider=True,
        layout_mode='split'
    )
    script, div = components(layout)
    return script, div

def make_sleep_bokeh_plot(df, df_ma, ma_lims=(1, 150)):
    df_ma = df_ma.copy()
    df_ma = trim_moving_average_range(df_ma, ma_lims[0], ma_lims[1])
    plot = InteractiveTimeSeriesPlot(
        df,
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
    plot.add_moving_average(df_ma, kernel='gaussian', bandwidth=14, add_sliders=True)
    layout = plot.build_layout(
        add_ma_controls=True,
        add_y_sliders=True,
        add_x_slider=True,
        layout_mode='split'
    )
    script, div = components(layout)
    return script, div

def make_steps_bokeh_plot(df, df_ma, ma_lims=(1, 150)):
    df_ma = df_ma.copy()
    df_ma = trim_moving_average_range(df_ma, ma_lims[0], ma_lims[1])
    plot = InteractiveTimeSeriesPlot(
        df,
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
    plot.add_moving_average(df_ma, kernel='gaussian', bandwidth=14, add_sliders=True)
    layout = plot.build_layout(
        add_ma_controls=True,
        add_y_sliders=True,
        add_x_slider=True,
        layout_mode='split'
    )
    script, div = components(layout)
    return script, div

def make_metric_bokeh_plot(metric, df, df_ma, ma_lims=(1, 150)):
    """
    Dispatch to the correct Bokeh plot function based on metric name.
    metric: str, e.g. 'health_stats', 'heart_rate', 'sleep', 'steps'
    df: main DataFrame
    df_ma: moving average DataFrame
    ma_lims: tuple (min, max) for moving average bandwidths
    """
    metric_map = {
        'health_stats': make_health_stats_bokeh_plot,
        'heart_rate': makeheart_rate_bokeh_plot,
        'sleep': make_sleep_bokeh_plot,
        'steps': make_steps_bokeh_plot,
    }
    if metric not in metric_map:
        raise ValueError(f"Unknown metric: {metric}")
    return metric_map[metric](df, df_ma, ma_lims)