"""
plotting.py: Bokeh plotting utilities for Garmin metrics dashboard
"""

from bokeh.embed import components
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from scipy.stats import gaussian_kde
from bokeh.embed import components
from bokeh.plotting import figure
from bokeh.models import LinearAxis, Range1d
from bokeh.layouts import row, column
import json
from bokeh.palettes import Category10
from bokeh.models import CustomJS, Slider, Select, ColumnDataSource, Div, RangeSlider
from bokeh.io import output_file, show, output_notebook


class InteractiveTimeSeriesPlot:
    def __init__(
        self,
        df,
        date_col,
        value_cols,
        y_axes=None,
        y_axis_labels=None,
        legend_labels=None,
        x_axis_label=None,
        show_plot=False,
        plot_width=1500,
        plot_height=350,
    ):
        self.df = df
        self.date_col = date_col
        self.value_cols = value_cols if isinstance(value_cols, list) else [value_cols]
        self.n = len(self.value_cols)
        self.y_axes = y_axes if y_axes is not None else ["default"] * self.n
        if len(self.y_axes) != self.n:
            if len(self.y_axes) == 1:
                self.y_axes = self.y_axes * self.n
            else:
                raise ValueError(
                    f"Length of y_axes ({len(self.y_axes)}) must be 1 or match number of value_cols ({self.n})"
                )
        self.y_axis_labels = y_axis_labels or {ax: ax for ax in set(self.y_axes)}
        self.legend_labels = legend_labels or {col: col for col in self.value_cols}
        self.x_axis_label = x_axis_label or "Date"
        self.show_plot = show_plot
        self.colors = Category10[10]
        self.p = figure(x_axis_type="datetime", width=plot_width, height=plot_height)
        self.p.xaxis.axis_label = None
        self.p.extra_y_ranges = {}
        self.p.extra_y_ranges["default"] = self.p.y_range
        self.p.yaxis.axis_label = self.y_axis_labels.get("default", "Value")
        self._setup_y_axes()
        self.scatter_source = ColumnDataSource(self.df)
        self.renderers = []
        self.ma_source = None
        self.kernel_select = None
        self.bandwidth_slider = None
        self.ma_callback = None
        self.x_slider = None
        self.x_label = None
        self.y_sliders = []
        self.y_sliders_row = None
        self.controls_row = None
        self.layout = None

    def _setup_y_axes(self):
        y_axis_set = list(dict.fromkeys(self.y_axes))
        for axis in y_axis_set:
            if axis == "default":
                continue
            cols_for_axis = [col for col, ax in zip(self.value_cols, self.y_axes) if ax == axis]
            vals = pd.concat([self.df[col] for col in cols_for_axis])
            rng = Range1d(start=vals.min(), end=vals.max())
            self.p.extra_y_ranges[axis] = rng
            self.p.add_layout(LinearAxis(y_range_name=axis, axis_label=self.y_axis_labels.get(axis, axis)), "right")

    def add_moving_average(self, df_ma, kernel=None, bandwidth=None, add_sliders=True):
        self.df_ma = df_ma
        # Infer kernels and bandwidths from column names in wide format
        import re
        param = self.value_cols[0]  # Use the first plotted parameter
        pattern = re.compile(rf"^{re.escape(param)}_(.*?)_(.*?)$")
        kernels = set()
        bandwidths = set()
        for col in df_ma.columns:
            m = pattern.match(col)
            if m:
                kernels.add(m.group(1))
                try:
                    bw = float(m.group(2))
                    if bw.is_integer():
                        bw = int(bw)
                    bandwidths.add(bw)
                except Exception:
                    pass
        self.kernels = sorted(kernels)
        self.bandwidths = sorted(bandwidths)
        self.initial_kernel = kernel or self.kernels[0]
        self.initial_bandwidth = bandwidth or self.bandwidths[0]
        self.ma_source = ColumnDataSource({})
        # Set initial data
        ma_data = {}
        for col in self.value_cols:
            colname = f"{col}_{self.initial_kernel}_{self.initial_bandwidth}"
            ma_data[col] = df_ma[colname].values if colname in df_ma.columns else [None]*len(df_ma)
        ma_data["date"] = pd.to_datetime(df_ma["date"]).values.astype(np.int64) // 10**6
        self.ma_source.data = ma_data
        self.ma_renderers = {}
        for idx, col in enumerate(self.value_cols):
            color = self.colors[idx % len(self.colors)]
            axis = self.y_axes[idx]
            line = self.p.line("date", col, color=color, y_range_name=axis, line_width=2, source=self.ma_source)
            self.ma_renderers[col] = line
        if add_sliders:
            kernel_select, bandwidth_slider, ma_callback = moving_average_controls(
                self.df_ma, self.value_cols, self.kernels, self.bandwidths, self.initial_kernel, self.initial_bandwidth, self.ma_source
            )
            # Patch the JS callback to preserve axis ranges (remove all .get_model_by_id/.references/.find usage)
            for cb in [kernel_select.js_property_callbacks, bandwidth_slider.js_property_callbacks]:
                for k, v in cb.items():
                    for c in v:
                        if hasattr(c, 'code') and 'ma_source.data = new_data;' in c.code:
                            c.code = c.code.replace(
                                'ma_source.data = new_data;',
                                (
                                    "// --- Save axis ranges ---\n"
                                    "function findFigure(obj) {\n"
                                    "    if (!obj) return null;\n"
                                    "    if (obj.type === 'Figure') return obj;\n"
                                    "    if (obj.children && obj.children.length) {\n"
                                    "        for (var i = 0; i < obj.children.length; ++i) {\n"
                                    "            var found = findFigure(obj.children[i]);\n"
                                    "            if (found) return found;\n"
                                    "        }\n"
                                    "    }\n"
                                    "    return null;\n"
                                    "}\n"
                                    "var fig = null;\n"
                                    "if (typeof Bokeh !== 'undefined' && Bokeh.documents.length > 0) {\n"
                                    "    var roots = Bokeh.documents[0].roots();\n"
                                    "    for (var i = 0; i < roots.length; ++i) {\n"
                                    "        fig = findFigure(roots[i]);\n"
                                    "        if (fig) break;\n"
                                    "    }\n"
                                    "}\n"
                                    "var xr_start = null, xr_end = null, yr_start = null, yr_end = null;\n"
                                    "if (fig) {\n"
                                    "    if (fig.x_range) { xr_start = fig.x_range.start; xr_end = fig.x_range.end; }\n"
                                    "    if (fig.y_range) { yr_start = fig.y_range.start; yr_end = fig.y_range.end; }\n"
                                    "}\n"
                                    "ma_source.data = new_data;\n"
                                    "ma_source.change.emit();\n"
                                    "// --- Restore axis ranges and set bounds to prevent auto-ranging ---\n"
                                    "if (fig) {\n"
                                    "    if (fig.x_range && xr_start !== null && xr_end !== null) {\n"
                                    "        fig.x_range.start = xr_start;\n"
                                    "        fig.x_range.end = xr_end;\n"
                                    "        fig.x_range.bounds = [xr_start, xr_end];\n"
                                    "    }\n"
                                    "    if (fig.y_range && yr_start !== null && yr_end !== null) {\n"
                                    "        fig.y_range.start = yr_start;\n"
                                    "        fig.y_range.end = yr_end;\n"
                                    "        fig.y_range.bounds = [yr_start, yr_end];\n"
                                    "    }\n"
                                    "}\n"
                                )
                            )
                            c.code = c.code + "\n" + (
                                "// --- Patch: robust Figure search for axis preservation ---\n"
                                "try {\n"
                                "    if (typeof Bokeh !== 'undefined' && Bokeh.documents.length > 0) {\n"
                                "        var fig = null;\n"
                                "        var roots = Bokeh.documents[0].roots();\n"
                                "        for (var i = 0; i < roots.length; ++i) {\n"
                                "            fig = findFigure(roots[i]);\n"
                                "            if (fig) break;\n"
                                "        }\n"
                                "        if (fig) {\n"
                                "            var xr = fig.x_range;\n"
                                "            var yr = fig.y_range;\n"
                                "            if (xr) {\n"
                                "                console.log('[MA DEBUG] x_range before:', xr.start, xr.end);\n"
                                "            }\n"
                                "            if (yr) {\n"
                                "                console.log('[MA DEBUG] y_range before:', yr.start, yr.end);\n"
                                "            }\n"
                                "            if (xr && xr.start !== undefined && xr.end !== undefined) {\n"
                                "                xr.change.emit();\n"
                                "            }\n"
                                "            if (yr && yr.start !== undefined && yr.end !== undefined) {\n"
                                "                yr.change.emit();\n"
                                "            }\n"
                                "            if (xr) {\n"
                                "                console.log('[MA DEBUG] x_range after:', xr.start, xr.end);\n"
                                "            }\n"
                                "            if (yr) {\n"
                                "                console.log('[MA DEBUG] y_range after:', yr.start, yr.end);\n"
                                "            }\n"
                                "        } else {\n"
                                "            console.log('[MA DEBUG] Could not find Figure for axis debug.');\n"
                                "        }\n"
                                "    }\n"
                                "} catch (err) {\n"
                                "    console.log('[MA DEBUG] JS error:', err);\n"
                                "}\n"
                            )
            # --- Store controls for layout ---
            self.kernel_select = kernel_select
            self.bandwidth_slider = bandwidth_slider
            self.ma_callback = ma_callback

    def add_scatter(self):
        self.scatter_renderers = {}
        for idx, col in enumerate(self.value_cols):
            color = self.colors[idx % len(self.colors)]
            axis = self.y_axes[idx]
            scatter = self.p.scatter(self.df[self.date_col], self.df[col], color=color, y_range_name=axis, size=4, alpha=0.1)
            self.scatter_renderers[col] = scatter
        # Do not add to self.renderers here; group in add_legend

    def add_legend(self):
        # Group scatter and line for each value_col under one legend label
        from bokeh.models import Legend
        items = []
        for col in self.value_cols:
            label = str(self.legend_labels.get(col, col))
            renderers = []
            if hasattr(self, 'scatter_renderers') and col in self.scatter_renderers:
                renderers.append(self.scatter_renderers[col])
            if hasattr(self, 'ma_renderers') and col in self.ma_renderers:
                renderers.append(self.ma_renderers[col])
            if renderers:
                items.append((label, renderers))
        legend = Legend(items=items)
        legend.orientation = "horizontal"
        legend.spacing = 10
        legend.margin = 0
        legend.padding = 5
        legend.location = "top_left"
        legend.click_policy = "hide"
        legend.ncols = len(items)
        self.p.add_layout(legend, "above")

    def add_y_sliders(self):
        y_axis_set = list(dict.fromkeys(self.y_axes))
        sliders = []
        for axis in y_axis_set:
            if axis == "default":
                cols_for_axis = [col for col, ax in zip(self.value_cols, self.y_axes) if ax == "default"]
                vals = pd.concat([self.df[col] for col in cols_for_axis])
                start = float(vals.min())
                end = float(vals.max())
                extent = end - start
                start -= 0.1 * extent
                end += 0.1 * extent
                extent = end - start
                slider_min = start - 0.2 * extent
                slider_max = end + 0.2 * extent
                self.p.y_range.start = start
                self.p.y_range.end = end
            else:
                y_range = self.p.extra_y_ranges[axis]
                start = y_range.start
                end = y_range.end
                extent = end - start
                slider_min = start - 0.2 * extent
                slider_max = end + 0.2 * extent
                y_range.start = start
                y_range.end = end
            if start is None or not np.isfinite(start):
                start = 0
            if end is None or not np.isfinite(end):
                end = start + 1
            if start == end:
                end = start + 1
            if slider_min is None or not np.isfinite(slider_min):
                slider_min = start
            if slider_max is None or not np.isfinite(slider_max):
                slider_max = end
            slider = RangeSlider(
                start=slider_min, end=slider_max,
                value=(start, end),
                step=(end - start) / 100 if end > start else 1,
                title=f"{self.y_axis_labels.get(axis, axis)} Range"
            )
            if axis == "default":
                callback = CustomJS(args=dict(rng=self.p.y_range, slider=slider), code="""
                    rng.start = slider.value[0];
                    rng.end = slider.value[1];
                """)
            else:
                callback = CustomJS(args=dict(rng=self.p.extra_y_ranges[axis], slider=slider), code="""
                    rng.start = slider.value[0];
                    rng.end = slider.value[1];
                """)
            slider.js_on_change("value", callback)
            sliders.append(slider)
        self.y_sliders = sliders
        self.y_sliders_row = row(*sliders, sizing_mode="stretch_width", spacing=20)

    def add_x_slider(self, add_slider=True):
        x_vals = pd.to_datetime(self.df[self.date_col])
        x_start = x_vals.min()
        x_end = x_vals.max()
        from bokeh.models import RangeSlider, Div, CustomJS

        def to_ms(val):
            if hasattr(val, "value"):
                return val.value // 10**6
            elif hasattr(val, "timestamp"):
                return int(val.timestamp() * 1000)
            elif isinstance(val, np.datetime64):
                return int(pd.to_datetime(val).value // 10**6)
            else:
                try:
                    return int(pd.to_datetime(val).value // 10**6)
                except Exception:
                    raise ValueError(f"Cannot convert {val} to ms since epoch")

        x_start_ms = to_ms(x_start)
        x_end_ms = to_ms(x_end)
        extent = x_end_ms - x_start_ms
        x_start_padded = x_start_ms - 0.05 * extent
        x_end_padded = x_end_ms + 0.05 * extent
        extent_padded = x_end_padded - x_start_padded
        slider_min = x_start_padded - 0.1 * extent_padded
        slider_max = x_end_padded + 0.1 * extent_padded
        # Set the plot's x_range to match the slider's initial range (with padding)
        self.p.x_range.start = x_start_padded
        self.p.x_range.end = x_end_padded

        def ms_to_str(ms):
            return pd.to_datetime(ms, unit="ms").strftime("%Y-%m-%d")

        initial_label = f"{self.x_axis_label} Range: {ms_to_str(x_start_padded)} .. {ms_to_str(x_end_padded)}"
        self.x_label = Div(text=initial_label, styles={"font-size": "14px", "margin-bottom": "4px"})
        if add_slider:
            self.x_slider = RangeSlider(
                start=slider_min, end=slider_max,
                value=(x_start_padded, x_end_padded),
                step=(x_end_ms - x_start_ms) / 100 if x_end_ms > x_start_ms else 1,
                title=None,
                sizing_mode="stretch_width"
            )
            self.x_slider.js_on_change("value", CustomJS(args=dict(xr=self.p.x_range, slider=self.x_slider, label=self.x_label, x_axis_label=self.x_axis_label), code="""
                function msToStr(ms) {
                    var d = new Date(ms);
                    return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
                }
                xr.start = slider.value[0];
                xr.end = slider.value[1];
                label.text = x_axis_label + ' Range: ' + msToStr(slider.value[0]) + ' .. ' + msToStr(slider.value[1]);
            """))
        else:
            self.x_slider = None

    def build_layout(self, add_ma_controls=True, add_y_sliders=True, add_x_slider=True, layout_config=None, custom_layout=None, layout_mode='default'):
        """
        Build and return the Bokeh layout for the interactive plot.
        layout_mode: 'default' (current behavior) or 'split' (two-column: left=controls+stacked y-sliders, right=plot+x-slider)
        If custom_layout is provided, it takes precedence.
        """
        self.add_scatter()
        if self.ma_source is not None:
            self.add_legend()
        if add_y_sliders:
            self.add_y_sliders()
        if add_x_slider:
            self.add_x_slider(add_slider=True)
        controls = []
        if self.kernel_select is not None:
            controls.append(self.kernel_select)
        if self.bandwidth_slider is not None:
            controls.append(self.bandwidth_slider)
        self.controls_row = row(*controls, sizing_mode="stretch_width", spacing=20) if controls else None
        # Map widget keys to objects
        widget_map = {
            'controls': self.controls_row,
            'kernel_select': self.kernel_select,
            'bandwidth_slider': self.bandwidth_slider,
            'y_sliders': self.y_sliders_row,
            'x_slider': self.x_slider,
            'x_label': self.x_label,
            'plot': self.p,
        }
        # Helper to build layout recursively
        def build_from_spec(spec):
            if isinstance(spec, str):
                return widget_map.get(spec)
            elif isinstance(spec, list):
                # If any element is a list, treat as grid (rows of columns)
                if any(isinstance(x, list) for x in spec):
                    # grid: each sublist is a row
                    rows = [build_from_spec(x) for x in spec]
                    return column(*[row(*r) if isinstance(r, (list, tuple)) else r for r in rows], sizing_mode="stretch_width")
                else:
                    # treat as column
                    widgets = [build_from_spec(x) for x in spec]
                    return column(*[w for w in widgets if w is not None], sizing_mode="stretch_width")
            else:
                return None
        if custom_layout is not None:
            self.layout = build_from_spec(custom_layout)
            if self.show_plot:
                show(self.layout)
            return self.layout
        # --- Fixed split layout option ---
        if layout_mode == 'split':
            left_col = []
            # Add section header for MA controls if present
            if self.kernel_select is not None or self.bandwidth_slider is not None:
                ma_header = Div(text="<b>Moving Average Parameters</b>")
                ma_header.styles = {"margin-bottom": "6px", "font-size": "15px"}
                left_col.append(ma_header)
            # PATCH: arrange controls vertically (kernel dropdown above bandwidth slider)
            if self.kernel_select is not None and self.bandwidth_slider is not None:
                controls_group = column(self.kernel_select, self.bandwidth_slider, sizing_mode="stretch_width", spacing=10)
                left_col.append(controls_group)
            elif self.controls_row is not None:
                left_col.append(self.controls_row)
            # Always add a more visible horizontal line for separation before Y-Axis Range(s)
            hr = Div(text='<hr style="border:none;border-top:2px solid #bbb;margin:18px 0 12px 0;">')
            left_col.append(hr)
            y_header = Div(text="<b>Y-Axis Range(s)</b>")
            y_header.styles = {"margin-bottom": "6px", "font-size": "15px"}
            left_col.append(y_header)
            if hasattr(self, 'y_sliders') and self.y_sliders:
                for s in self.y_sliders:
                    left_col.append(s)
            left = column(*left_col, sizing_mode="stretch_width", max_width=350) if left_col else None
            # Right: plot, then x-slider (and label if present)
            # PATCH: make plot fill width but not overflow
            self.p.sizing_mode = "stretch_width"
            self.p.max_width = 1200  # Cap plot width
            right_col = [self.p]
            x_slider_group = None
            if add_x_slider and self.x_slider is not None and self.x_label is not None:
                x_slider_group = column(self.x_slider, self.x_label, sizing_mode="stretch_width")
            elif add_x_slider and self.x_slider is not None:
                x_slider_group = self.x_slider
            elif add_x_slider and self.x_label is not None:
                x_slider_group = self.x_label
            if x_slider_group is not None:
                right_col.append(x_slider_group)
            right = column(*right_col, sizing_mode="stretch_width", max_width=1200)
            # Add spacing between left and right columns, and cap total width
            self.layout = row(left, right, sizing_mode="stretch_width", spacing=30, max_width=1400) if left is not None else right
            if self.show_plot:
                show(self.layout)
            return self.layout
        # --- Fallback to original layout_config logic ---
        if layout_config is None:
            layout_config = {'controls': 'top', 'y_sliders': 'bottom', 'x_slider': 'bottom'}
        # Prepare layout items
        items = {
            'controls': self.controls_row,
            'plot': self.p,
            'x_slider': self.x_slider,
            'x_label': self.x_label,
            'y_sliders': self.y_sliders_row,
        }
        # Compose layout
        # Start with plot
        main = items['plot']
        # Place x_slider and label
        x_slider_group = None
        if add_x_slider and items['x_slider'] is not None and items['x_label'] is not None:
            x_slider_group = column(items['x_slider'], items['x_label'], sizing_mode="stretch_width")
        elif add_x_slider and items['x_slider'] is not None:
            x_slider_group = items['x_slider']
        elif add_x_slider and items['x_label'] is not None:
            x_slider_group = items['x_label']
        if x_slider_group is not None:
            if layout_config.get('x_slider', 'bottom') == 'top':
                main = column(x_slider_group, main, sizing_mode="stretch_width")
            elif layout_config.get('x_slider') == 'bottom':
                main = column(main, x_slider_group, sizing_mode="stretch_width")
            elif layout_config.get('x_slider') == 'left':
                main = row(x_slider_group, main, sizing_mode="stretch_width")
            elif layout_config.get('x_slider') == 'right':
                main = row(main, x_slider_group, sizing_mode="stretch_width")
        # Place y_sliders
        if add_y_sliders and items['y_sliders'] is not None:
            if layout_config.get('y_sliders', 'right') == 'left':
                main = row(items['y_sliders'], main, sizing_mode="stretch_width")
            elif layout_config.get('y_sliders', 'right') == 'right':
                main = row(main, items['y_sliders'], sizing_mode="stretch_width")
            elif layout_config.get('y_sliders') == 'top':
                main = column(items['y_sliders'], main, sizing_mode="stretch_width")
            elif layout_config.get('y_sliders') == 'bottom':
                main = column(main, items['y_sliders'], sizing_mode="stretch_width")
        # Place controls
        controls = items['controls']
        if add_ma_controls and controls is not None:
            if layout_config.get('controls', 'top') == 'top':
                main = column(controls, main, sizing_mode="stretch_width")
            elif layout_config.get('controls') == 'bottom':
                main = column(main, controls, sizing_mode="stretch_width")
            elif layout_config.get('controls') == 'left':
                main = row(controls, main, sizing_mode="stretch_width")
            elif layout_config.get('controls') == 'right':
                main = row(main, controls, sizing_mode="stretch_width")
        self.layout = main
        if self.show_plot:
            show(self.layout)
        return self.layout
    def compute_moving_average(self, kernels, bandwidths, yerr_col=None, x_out=None):
        """
        Compute moving averages for all value_cols for each kernel/bandwidth combination using kernel_smooth_with_uncertainty.
        Returns a DataFrame with columns: date, {param}_{kernel}_{bandwidth} for each param/kernel/bandwidth.
        """
        from myutils.utils import kernel_smooth_with_uncertainty
        if x_out is None:
            x_out = self.df[self.date_col]
        if not isinstance(kernels, list):
            kernels = [kernels]
        if not isinstance(bandwidths, list):
            bandwidths = [bandwidths]
        df_out = pd.DataFrame({"date": pd.to_datetime(x_out)})
        for kernel in kernels:
            for bandwidth in bandwidths:
                for col in self.value_cols:
                    smoothed, _ = kernel_smooth_with_uncertainty(
                        self.df, self.date_col, col, yerr_col=yerr_col, kernel=kernel, bandwidth=bandwidth, x_out=x_out
                    )
                    df_out[f"{col}_{kernel}_{bandwidth}"] = smoothed
        return df_out


def moving_average_controls(df_ma, value_cols, kernels, bandwidths, initial_kernel, initial_bandwidth, ma_source):
    """
    Returns (kernel_select, bandwidth_slider, callback) for interactive moving average controls.
    """
    from bokeh.models import CustomJS, Slider, Select
    import json

    kernel_select = Select(title="Kernel", value=str(initial_kernel), options=[str(k) for k in kernels])
    bandwidths_sorted = sorted(bandwidths)
    bandwidth_slider = Slider(
        title=f"Bandwidth: {initial_bandwidth}",
        start=0,
        end=len(bandwidths_sorted) - 1,
        value=bandwidths_sorted.index(initial_bandwidth),
        step=1,
    )
    # --- Patch: force the bandwidth slider label in the DOM after every change, robustly ---
    from bokeh.models import CustomJS
    bandwidth_slider.js_on_change("value", CustomJS(args=dict(slider=bandwidth_slider, bandwidths=bandwidths_sorted), code="""
        var idx = slider.value;
        var bw = bandwidths[idx];
        slider.title = 'Bandwidth: ' + bw;
        setTimeout(function() {
            var labels = document.querySelectorAll('.bk-slider-title, label[for]');
            for (var i = 0; i < labels.length; ++i) {
                if (labels[i].textContent.includes('Bandwidth:')) {
                    labels[i].textContent = 'Bandwidth: ' + bw;
                }
                if (labels[i].getAttribute && labels[i].getAttribute('aria-label') && labels[i].getAttribute('aria-label').includes('Bandwidth:')) {
                    labels[i].setAttribute('aria-label', 'Bandwidth: ' + bw);
                }
            }
        }, 20);
    """))
    # Prepare JS data for wide format
    js_ma_data = {col: df_ma[col].tolist() for col in df_ma.columns if col != 'date'}
    js_ma_data["date"] = (pd.to_datetime(df_ma["date"]).astype(np.int64) // 10**6).tolist()
    js_ma_data_json = json.dumps(js_ma_data)
    value_cols_json = json.dumps(value_cols)
    js_kernels_json = json.dumps([str(k) for k in kernels])
    js_bandwidths_json = json.dumps([int(b) for b in bandwidths_sorted])
    js_code = """
const kernel = kernel_select.value;
const bandwidths = %s;
const bw_idx = bandwidth_slider.value;
var bandwidth = bandwidths[bw_idx];
bandwidth_slider.title = 'Bandwidth: ' + bandwidth;
setTimeout(function() {
    var labels = document.querySelectorAll('.bk-slider-title, label[for]');
    for (var i = 0; i < labels.length; ++i) {
        if (labels[i].textContent.includes('Bandwidth:')) {
            labels[i].textContent = 'Bandwidth: ' + bandwidth;
        }
        if (labels[i].getAttribute && labels[i].getAttribute('aria-label') && labels[i].getAttribute('aria-label').includes('Bandwidth:')) {
            labels[i].setAttribute('aria-label', 'Bandwidth: ' + bandwidth);
        }
    }
}, 20);
const all_data = %s;
const value_cols = %s;
var new_data = {};
new_data['date'] = all_data['date'];
for (var i = 0; i < value_cols.length; ++i) {
    var param = value_cols[i];
    var colname = param + '_' + kernel + '_' + bandwidth;
    new_data[param] = all_data[colname];
}
ma_source.data = new_data;
ma_source.change.emit();
""" % (js_bandwidths_json, js_ma_data_json, value_cols_json)

    callback = CustomJS(
        args=dict(ma_source=ma_source, kernel_select=kernel_select, bandwidth_slider=bandwidth_slider),
        code=js_code,
    )
    kernel_select.js_on_change("value", callback)
    bandwidth_slider.js_on_change("value", callback)
    bandwidth_slider.title = f"Bandwidth: {bandwidths_sorted[bandwidth_slider.value]}"
    return kernel_select, bandwidth_slider, callback

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