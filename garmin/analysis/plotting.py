"""
plotting.py: Bokeh plotting utilities for Garmin metrics dashboard
"""

import time
from bokeh.plotting import figure
from bokeh.embed import components
from bokeh.models import ColumnDataSource, Range1d, Slider, Select, CustomJS, Legend
from bokeh.layouts import column as bokeh_column
import pandas as pd
import math
from garmin.analysis.analysis import get_or_compute_moving_averages
import os
import pickle

def cache_bokeh_plot(key, script, div, cache_dir='/tmp', max_age=86400):
    path = os.path.join(cache_dir, f"{key}_bokeh.pkl")
    with open(path, 'wb') as f:
        pickle.dump({'script': script, 'div': div, 'timestamp': time.time()}, f)

def load_bokeh_plot(key, cache_dir='/tmp', max_age=86400):
    path = os.path.join(cache_dir, f"{key}_bokeh.pkl")
    if os.path.exists(path):
        with open(path, 'rb') as f:
            data = pickle.load(f)
        if time.time() - data['timestamp'] < max_age:
            return data['script'], data['div']
    return None, None

def make_metric_bokeh_plot(
    df,
    metrics,
    date_col='date',
    y_axes=None,
    y_axis_labels=None,
    y_axis_ranges=None,
    moving_average=True,
    ma_widths=None,
    ma_type='gaussian',
    sizing_mode="stretch_width",
    width=1200,
    height=300,
    cache_key=None,
    cache_dir='/tmp',
    cache_max_age=86400,
    force_refresh=False
):
    if cache_key and not force_refresh:
        script, div = load_bokeh_plot(cache_key, cache_dir, cache_max_age)
        if script is not None and div is not None:
            print(f"[CACHE] Loaded Bokeh plot for {cache_key}")
            return script, div, None
    t0 = time.time()
    if ma_widths is None:
        ma_widths = [1] + [i for i in range(7, 181, 7)]
    if ma_type not in ('gaussian', 'simple'):
        ma_type = 'gaussian'
    if y_axes is None:
        y_axes = ['default']
    if y_axis_labels is None:
        y_axis_labels = {ax: ax for ax in y_axes}
    if y_axis_ranges is None:
        y_axis_ranges = {}

    # Prepare data and moving averages
    t1 = time.time()
    data = {date_col: [pd.Timestamp(d).to_pydatetime() for d in df[date_col]]}
    for m in metrics:
        col = m['col']
        data[col] = df[col].astype(float).tolist()
        if moving_average and m.get('ma', True):
            # Use cached or computed moving averages from analysis.py
            cache_path = f"/tmp/ma_cache_{col}.pkl"
            ma_simple, ma_gaussian = get_or_compute_moving_averages(df, col, ma_widths, cache_path)
            for w in ma_widths:
                data[f'{col}_ma_simple_{w}'] = ma_simple[w].astype(float).tolist()
                data[f'{col}_ma_gaussian_{w}'] = ma_gaussian[w].astype(float).tolist()
    t2 = time.time()
    print(f"[TIMER] Data prep and moving averages: {t2-t1:.3f} s")
    source = ColumnDataSource(data)

    # Set up y-axis ranges
    t3 = time.time()
    y_ranges = {}
    for ax in y_axes:
        if ax in y_axis_ranges and y_axis_ranges[ax] is not None:
            y_ranges[ax] = Range1d(start=y_axis_ranges[ax][0], end=y_axis_ranges[ax][1])
        else:
            # Auto-range from data
            vals = []
            for m in metrics:
                if m.get('y_axis', 'default') == ax:
                    vals += [v for v in df[m['col']].values if v is not None and not (isinstance(v, float) and math.isnan(v))]
            if vals:
                minv, maxv = min(vals), max(vals)
                pad = (maxv - minv) * 0.1 if maxv > minv else 1
                y_ranges[ax] = Range1d(start=minv - pad, end=maxv + pad)
            else:
                y_ranges[ax] = Range1d(start=0, end=1)
    t4 = time.time()
    print(f"[TIMER] Y-axis range setup: {t4-t3:.3f} s")

    # Create figure
    t5 = time.time()
    x_max = pd.Timestamp(df[date_col].max())
    x_min = pd.Timestamp(df[date_col].min())
    x_extent = x_max - x_min
    x_min -= x_extent * 0.05
    x_max += x_extent * 0.05

    p = figure(
        width=width, height=height, x_axis_type='datetime',
        tools="pan,xwheel_zoom,box_zoom,reset,save", toolbar_location="above",
        sizing_mode=sizing_mode,
        y_range=y_ranges[y_axes[0]],
        x_range=Range1d(start=x_min, end=x_max),
        output_backend="webgl"  # Enable WebGL for faster rendering
    )
    if len(y_axes) > 1:
        for ax in y_axes[1:]:
            p.extra_y_ranges = p.extra_y_ranges or {}
            p.extra_y_ranges[ax] = y_ranges[ax]
            p.add_layout(p.yaxis[0].clone(y_range_name=ax), 'right')
    for i, ax in enumerate(y_axes):
        p.yaxis[i].axis_label = y_axis_labels.get(ax, ax)

    if moving_average:
        default_ma_value = 28 if 28 in ma_widths else ma_widths[0]
    # Plot scatter and moving averages, collect renderers for custom legend
    ma_lines = {}
    scatter_renderers = {}
    legend_items = []
    for m in metrics:
        col = m['col']
        color = m['color']
        label = m['label']
        y_axis = m.get('y_axis', y_axes[0])
        name = m.get('name', col)
        # Moving average line (initial)
        ma_line = None
        if moving_average and m.get('ma', True):
            ma_col = f'{col}_ma_{ma_type}_{default_ma_value}'
            ma_line = p.line(
                date_col, ma_col, source=source, line_width=2, color=color, alpha=1.0, name=f"{name}_ma_line",
                **({'y_range_name': y_axis} if y_axis != y_axes[0] else {})
            )
            ma_lines[name] = ma_line
        # Scatter
        scatter = p.scatter(
            date_col, col, source=source, size=6, color=color, alpha=0.1, name=name,
            **({'y_range_name': y_axis} if y_axis != y_axes[0] else {})
        )
        scatter_renderers[name] = scatter
        # Custom legend: one entry per metric, both scatter and MA line
        renderers = [scatter]
        if ma_line:
            renderers.append(ma_line)
        legend_items.append((label, renderers))

    # Hide default legend and add custom legend above the plot, in a single row (n columns)
    if p.legend:
        p.legend.visible = False
    from bokeh.models import Legend
    custom_legend = Legend(
        items=legend_items,
        location="top_left",
        orientation="horizontal",
        click_policy="hide",
        label_text_font_size="14px",
        spacing=10,
        margin=0,
        padding=0
    )
    p.add_layout(custom_legend, 'above')

    # Moving average controls
    controls = []
    # --- Y-axis range sliders ---
    from bokeh.models import RangeSlider
    from bokeh.layouts import row as bokeh_row
    y_range_sliders = []
    y_range_slider_callbacks = []
    for i, ax in enumerate(y_axes):
        # Get min/max from data for this axis
        vals = []
        for m in metrics:
            if m.get('y_axis', 'default') == ax:
                vals += [v for v in df[m['col']].values if v is not None and not (isinstance(v, float) and math.isnan(v))]
        if vals:
            minv, maxv = min(vals), max(vals)
            pad = (maxv - minv) * 0.1 if maxv > minv else 1
            slider = RangeSlider(
                start=minv - pad, end=maxv + pad,
                value=(y_ranges[ax].start, y_ranges[ax].end), step=(maxv-minv)/200 if maxv>minv else 1,
                title=f"{y_axis_labels.get(ax, ax)} Range", width=350
            )
            # JS callback to update y_range
            if i == 0:
                # Left axis
                callback = CustomJS(args=dict(rng=p.y_range, slider=slider), code="""
                    rng.start = slider.value[0];
                    rng.end = slider.value[1];
                """)
            else:
                # Right or extra axis
                callback = CustomJS(args=dict(rng=p.extra_y_ranges[ax], slider=slider), code="""
                    rng.start = slider.value[0];
                    rng.end = slider.value[1];
                """)
            slider.js_on_change('value', callback)
            y_range_sliders.append(slider)
    if y_range_sliders:
        controls.append(bokeh_column(*y_range_sliders, sizing_mode="scale_width"))
    # --- End y-axis range sliders ---
    # --- X-axis range slider ---
    from bokeh.models import DateRangeSlider
    if len(df) > 0:
        x_slider = DateRangeSlider(
            title="Date Range",
            start=x_min,
            end=x_max,
            value=(x_min, x_max),
            step=1,  # step in days
            width=width,
            sizing_mode=None,
        )
        # JS callback to update x_range
        x_slider.js_on_change('value', CustomJS(args=dict(xr=p.x_range, slider=x_slider), code="""
            // Bokeh DateRangeSlider value is [ms, ms], but x_range expects ms since epoch (float)
            xr.start = slider.value[0];
            xr.end = slider.value[1];
            xr.change.emit();
        """))
    else:
        x_slider = None
    # --- End x-range slider ---
    if moving_average:
        slider = Slider(start=min(ma_widths), end=max(ma_widths), value=default_ma_value, step=1, title="Moving Average Width", width=180)
        select_widget = Select(title="Type", value=ma_type, options=[("gaussian", "Gaussian"), ("simple", "Simple")])
        controls.extend([slider, select_widget])
        # Snap slider to allowed values only
        slider.js_on_change('value', CustomJS(args=dict(slider=slider), code=f"""
            var allowed = {ma_widths};
            var v = slider.value;
            var closest = allowed.reduce((a, b) => Math.abs(b - v) < Math.abs(a - v) ? b : a);
            if (v !== closest) {{ slider.value = closest; }}
        """))
        # JS callback for all MA lines
        callback_code = """
        var w = slider.value;
        var t = select_widget.value;
        """
        for m in metrics:
            if moving_average and m.get('ma', True):
                name = m.get('name', m['col'])
                col = m['col']
                callback_code += f"{name}_ma_line.glyph.y = {{ field: '{col}_ma_' + t + '_' + w }}; {name}_ma_line.change.emit();\n"
        callback_code += "source.change.emit();"
        callback = CustomJS(args={
            'source': source,
            'slider': slider,
            'select_widget': select_widget,
            **{f"{m.get('name', m['col'])}_ma_line": ma_lines[m.get('name', m['col'])] for m in metrics if moving_average and m.get('ma', True)}
        }, code=callback_code)
        slider.js_on_change('value', callback)
        select_widget.js_on_change('value', callback)
    # Layout
    layout_children = [p]
    if x_slider:
        layout_children.append(x_slider)
    if controls:
        layout_children.extend(controls)
    layout = bokeh_column(*layout_children, sizing_mode="scale_width")
    script, div = components(layout)
    t6 = time.time()
    print(f"[TIMER] Bokeh plot creation and layout: {t6-t5:.3f} s")
    print(f"[TIMER] Total make_metric_bokeh_plot: {t6-t0:.3f} s")
    if cache_key:
        cache_bokeh_plot(cache_key, script, div, cache_dir, cache_max_age)
        print(f"[CACHE] Saved Bokeh plot for {cache_key}")
    return script, div, None
