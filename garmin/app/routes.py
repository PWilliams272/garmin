from flask import Blueprint, render_template
from garmin.io.file_manager import FileManager

bp = Blueprint(
    'weight',
    __name__,
    template_folder="templates",
    static_folder="static"
)

fm = FileManager()

@bp.route('/metrics_dashboard')
def metrics_dashboard():
    """
    Metrics dashboard route that generates and displays interactive time series plots
    for weight, heart rate, sleep, and steps metrics.
    """
    health_stats_script = fm.read_text("dashboards/metric_timeseries/health_stats_timeseries_script.html")
    health_stats_div = fm.read_text("dashboards/metric_timeseries/health_stats_timeseries_div.html")
    heart_rate_script = fm.read_text("dashboards/metric_timeseries/heart_rate_timeseries_script.html")
    heart_rate_div = fm.read_text("dashboards/metric_timeseries/heart_rate_timeseries_div.html")
    sleep_script = fm.read_text("dashboards/metric_timeseries/sleep_timeseries_script.html")
    sleep_div = fm.read_text("dashboards/metric_timeseries/sleep_timeseries_div.html")
    steps_script = fm.read_text("dashboards/metric_timeseries/steps_timeseries_script.html")
    steps_div = fm.read_text("dashboards/metric_timeseries/steps_timeseries_div.html")

    return render_template(
        'metrics_dashboard.html',
        bokeh_script_weight_timeseries=health_stats_script,
        bokeh_div_weight_timeseries=health_stats_div,
        bokeh_script_hr_timeseries=heart_rate_script,
        bokeh_div_hr_timeseries=heart_rate_div,
        bokeh_script_sleep_timeseries=sleep_script,
        bokeh_div_sleep_timeseries=sleep_div,
        bokeh_script_steps_timeseries=steps_script,
        bokeh_div_steps_timeseries=steps_div
    )