"""
Lambda handler for triggering Garmin data update from AWS Lambda.
"""
from garmin.updaters import DataUpdater
from garmin.io.db_manager import DatabaseManager
from garmin.api import GarminSession
from garmin.data_processor.processor import GarminDataProcessor
from garmin.io.file_manager import FileManager
from garmin.analysis.plotting import make_metric_bokeh_plot

import boto3
import json

def emit_event(step_result):
    client = boto3.client('events')
    client.put_events(
        Entries=[{
            'Source': 'custom.garmin.workflow',
            'DetailType': 'StepComplete',
            'Detail': json.dumps({'status': step_result}),
            'EventBusName': 'default'
        }]
    )

def lambda_handler(event, context):
    step = event.get('step')
    if step == 'step 1':
        db_manager = DatabaseManager()
        session = GarminSession()
        updater = DataUpdater(session=session, db_manager=db_manager)
        updater.update_all()
        emit_event('step 1 complete')
        return {"status": "step 1 complete"}
    elif step == 'step 2':
        db_manager = DatabaseManager()
        proc = GarminDataProcessor()
        fm = FileManager()

        # Load raw data from the database
        raw_data_dict = {
            'health_stats': db_manager.get_df('health_stats'),
            'sleep': db_manager.get_df('sleep'),
            'steps': db_manager.get_df('steps'),
            'stress': db_manager.get_df('stress'),
            'heart_rate': db_manager.get_df('heart_rate'),
            'body_battery': db_manager.get_df('body_battery'),
        }
        processed_data = proc.process_all(raw_data_dict)
        for k, v in processed_data.items():
            print("Saving processed data for:", k)
            fn = f"processed/{k}.parquet"
            fm.write_df(v, fn, format='parquet')

        moving_averages = proc.calculate_moving_averages_all(
            processed_data,
            kernels=['gaussian', 'boxcar'],
            bandwidths=[1] + list(range(7, 150, 7))
        )
        for k, v in moving_averages.items():
            print("Saving moving averages for:", k)
            fn = f"moving_averages/{k}.parquet"
            fm.write_df(v, fn, format='parquet')
        emit_event('step 2 complete')
        return {"status": "step 2 complete"}
    elif step == 'step 3':
        fm = FileManager()
        moving_average_lims = (0, 150)
        metrics = ['health_stats', 'heart_rate', 'sleep', 'steps']
        for metric in metrics:
            df = fm.read_df(f'processed/{metric}.parquet', format='parquet')
            df_ma = fm.read_df(f'moving_averages/{metric}.parquet', format='parquet')
            script, div = make_metric_bokeh_plot(metric, df, df_ma, ma_lims=moving_average_lims)
            fm.write_text(script, f"dashboards/metric_timeseries/{metric}_timeseries_script.html")
            fm.write_text(div, f"dashboards/metric_timeseries/{metric}_timeseries_div.html")
        emit_event('step 3 complete')
        return {"status": "step 3 complete"}
    else:
        return {'error': 'Unknown step'}