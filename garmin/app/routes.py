# garmin/app/routes.py
from flask import Blueprint, render_template, request
from garmin.analysis.plotting import make_metric_bokeh_plot
import pandas as pd
from garmin.db.db_manager import get_db_session
from garmin.db.models import HealthStats, Steps, Sleep, HeartRate, Stress, BodyBattery

bp = Blueprint(
    'weight',
    __name__,
    template_folder="templates",
    static_folder="static"
)

@bp.route('/metrics_dashboard')
def metrics_dashboard_route():
    force_refresh = request.args.get('refresh', '0') == '1'
    # Weight + body fat
    session = get_db_session()
    weights = session.query(HealthStats).order_by(HealthStats.date).all()
    session.close()
    df_weight = pd.DataFrame([
        {'date': w.date, 'weight': w.weight, 'body_fat': getattr(w, 'body_fat', None)} for w in weights
    ])
    metrics_weight = [
        {'col': 'weight', 'label': 'Weight', 'color': '#1976d2', 'y_axis': 'default', 'name': 'weight_line'},
        {'col': 'body_fat', 'label': 'Body Fat %', 'color': '#d32f2f', 'y_axis': 'body_fat', 'name': 'bf_line'}
    ]
    y_axes_weight = ['default', 'body_fat']
    y_axis_labels_weight = {'default': 'Weight (kg)', 'body_fat': 'Body Fat %'}
    weight_script, weight_div, weight_msg = make_metric_bokeh_plot(
        df_weight, metrics_weight,
        y_axes=y_axes_weight, y_axis_labels=y_axis_labels_weight, moving_average=True,
        cache_key="weight_plot", force_refresh=force_refresh
    )

    # Steps
    session = get_db_session()
    steps = session.query(Steps).order_by(Steps.date).all()
    session.close()
    df_steps = pd.DataFrame([
        {'date': s.date, 'total_steps': s.total_steps, 'step_goal': s.step_goal} for s in steps
    ])
    metrics_steps = [
        {'col': 'total_steps', 'label': 'Total Steps', 'color': '#388e3c', 'name': 'total_steps'},
        {'col': 'step_goal', 'label': 'Step Goal', 'color': '#fbc02d', 'name': 'step_goal'}
    ]
    steps_script, steps_div, steps_msg = make_metric_bokeh_plot(
        df_steps, metrics_steps, moving_average=True,
        cache_key="steps_plot", force_refresh=force_refresh
    )

    # Sleep
    session = get_db_session()
    sleep = session.query(Sleep).order_by(Sleep.date).all()
    session.close()
    df_sleep = pd.DataFrame([
        {'date': s.date, 'total_sleep_time': s.total_sleep_time, 'deep_time': s.deep_time, 'rem_time': s.rem_time, 'light_time': s.light_time, 'awake_time': s.awake_time, 'sleep_score': s.sleep_score} for s in sleep
    ])
    metrics_sleep = [
        {'col': 'total_sleep_time', 'label': 'Total Sleep', 'color': '#1976d2', 'name': 'total_sleep_time'},
        {'col': 'deep_time', 'label': 'Deep Sleep', 'color': '#512da8', 'name': 'deep_time'},
        {'col': 'rem_time', 'label': 'REM Sleep', 'color': '#c2185b', 'name': 'rem_time'},
        {'col': 'light_time', 'label': 'Light Sleep', 'color': '#0288d1', 'name': 'light_time'},
        {'col': 'awake_time', 'label': 'Awake Time', 'color': '#fbc02d', 'name': 'awake_time'},
        {'col': 'sleep_score', 'label': 'Sleep Score', 'color': '#388e3c', 'name': 'sleep_score'}
    ]
    sleep_script, sleep_div, sleep_msg = make_metric_bokeh_plot(
        df_sleep, metrics_sleep, moving_average=True,
        cache_key="sleep_plot", force_refresh=force_refresh
    )

    # Heart rate
    session = get_db_session()
    hr = session.query(HeartRate).order_by(HeartRate.date).all()
    session.close()
    df_hr = pd.DataFrame([
        {'date': h.date, 'resting_hr': h.resting_hr, 'max_avg_hr': h.wellness_max_avg_hr, 'min_avg_hr': h.wellness_min_avg_hr} for h in hr
    ])
    metrics_hr = [
        {'col': 'resting_hr', 'label': 'Resting HR', 'color': '#d32f2f', 'name': 'resting_hr'},
        {'col': 'max_avg_hr', 'label': 'Max Avg HR', 'color': '#1976d2', 'name': 'max_avg_hr'},
        {'col': 'min_avg_hr', 'label': 'Min Avg HR', 'color': '#388e3c', 'name': 'min_avg_hr'}
    ]
    hr_script, hr_div, hr_msg = make_metric_bokeh_plot(
        df_hr, metrics_hr, moving_average=True,
        cache_key="hr_plot", force_refresh=force_refresh
    )

    # Stress
    session = get_db_session()
    stress = session.query(Stress).order_by(Stress.date).all()
    session.close()
    df_stress = pd.DataFrame([
        {'date': s.date, 'overall_stress_level': s.overall_stress_level, 'high_stress_duration': s.high_stress_duration, 'low_stress_duration': s.low_stress_duration, 'rest_stress_duration': s.rest_stress_duration} for s in stress
    ])
    metrics_stress = [
        {'col': 'overall_stress_level', 'label': 'Overall Stress Level', 'color': '#1976d2', 'name': 'overall_stress_level'},
        {'col': 'high_stress_duration', 'label': 'High Stress Duration', 'color': '#d32f2f', 'name': 'high_stress_duration'},
        {'col': 'low_stress_duration', 'label': 'Low Stress Duration', 'color': '#388e3c', 'name': 'low_stress_duration'},
        {'col': 'rest_stress_duration', 'label': 'Rest Stress Duration', 'color': '#fbc02d', 'name': 'rest_stress_duration'}
    ]
    stress_script, stress_div, stress_msg = make_metric_bokeh_plot(
        df_stress, metrics_stress, moving_average=True,
        cache_key="stress_plot", force_refresh=force_refresh
    )

    # Body battery
    session = get_db_session()
    bb = session.query(BodyBattery).order_by(BodyBattery.date).all()
    session.close()
    df_bb = pd.DataFrame([
        {'date': b.date, 'low_body_battery': b.low_body_battery, 'high_body_battery': b.high_body_battery} for b in bb
    ])
    metrics_bb = [
        {'col': 'low_body_battery', 'label': 'Low Body Battery', 'color': '#1976d2', 'name': 'low_body_battery'},
        {'col': 'high_body_battery', 'label': 'High Body Battery', 'color': '#388e3c', 'name': 'high_body_battery'}
    ]
    bb_script, bb_div, bb_msg = make_metric_bokeh_plot(
        df_bb, metrics_bb, moving_average=True,
        cache_key="bb_plot", force_refresh=force_refresh
    )

    return render_template(
        'metrics_dashboard.html',
        weight_script=weight_script, weight_div=weight_div, weight_msg=weight_msg,
        steps_script=steps_script, steps_div=steps_div, steps_msg=steps_msg,
        sleep_script=sleep_script, sleep_div=sleep_div, sleep_msg=sleep_msg,
        hr_script=hr_script, hr_div=hr_div, hr_msg=hr_msg,
        stress_script=stress_script, stress_div=stress_div, stress_msg=stress_msg,
        bb_script=bb_script, bb_div=bb_div, bb_msg=bb_msg,
        force_refresh=force_refresh
    )
