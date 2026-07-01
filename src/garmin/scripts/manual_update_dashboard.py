from dotenv import load_dotenv
load_dotenv()

from garmin.io.file_manager import FileManager
from garmin.analysis.plotting import make_metric_bokeh_plot

def main():
    fm = FileManager()
    moving_average_lims = (0, 150)
    metrics = ['health_stats', 'heart_rate', 'sleep', 'steps']
    for metric in metrics:
        df = fm.read_df(f'processed/{metric}.parquet', format='parquet')
        df_ma = fm.read_df(f'moving_averages/{metric}.parquet', format='parquet')
        script, div = make_metric_bokeh_plot(metric, df, df_ma, ma_lims=moving_average_lims)
        fm.write_text(script, f"dashboards/metric_timeseries/{metric}_timeseries_script.html")
        fm.write_text(div, f"dashboards/metric_timeseries/{metric}_timeseries_div.html")

if __name__ == "__main__":
    main()