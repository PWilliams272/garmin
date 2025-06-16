from flask import Blueprint, render_template, request, url_for, redirect
from garmin.io.file_manager import FileManager
import os

bp = Blueprint(
    'garmin',
    __name__,
    template_folder="templates",
    static_folder="static"
)

fm_local = FileManager(environment='local')
fm_s3 = FileManager(environment='aws')

DASHBOARD_FILES = [
    ("health_stats_timeseries_script.html", "bokeh_script_weight_timeseries"),
    ("health_stats_timeseries_div.html", "bokeh_div_weight_timeseries"),
    ("heart_rate_timeseries_script.html", "bokeh_script_hr_timeseries"),
    ("heart_rate_timeseries_div.html", "bokeh_div_hr_timeseries"),
    ("sleep_timeseries_script.html", "bokeh_script_sleep_timeseries"),
    ("sleep_timeseries_div.html", "bokeh_div_sleep_timeseries"),
    ("steps_timeseries_script.html", "bokeh_script_steps_timeseries"),
    ("steps_timeseries_div.html", "bokeh_div_steps_timeseries"),
]

@bp.route('/')
def index():
    # Redirect /garmin to /garmin/metrics_dashboard
    return redirect(url_for('garmin.metrics_dashboard'))

@bp.route('/metrics_dashboard')
def metrics_dashboard():
    refresh = request.args.get('refresh', '0') == '1'
    dashboard_dir = os.path.join(fm_local.local_dir, "dashboards/metric_timeseries")
    os.makedirs(dashboard_dir, exist_ok=True)
    context = {}
    for fname, context_key in DASHBOARD_FILES:
        local_path = os.path.join(dashboard_dir, fname)
        if refresh or not os.path.exists(local_path):
            # Fetch from S3 and save locally
            text = fm_s3.read_text(f"dashboards/metric_timeseries/{fname}")
            with open(local_path, 'w', encoding='utf-8') as f:
                f.write(text)
        else:
            with open(local_path, 'r', encoding='utf-8') as f:
                text = f.read()
        context[context_key] = text
    return render_template(
        'metrics_dashboard.html',
        **context
    )