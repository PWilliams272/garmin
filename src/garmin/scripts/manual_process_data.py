from dotenv import load_dotenv
load_dotenv()

from garmin.data_processor.processor import GarminDataProcessor
from garmin.io.db_manager import DatabaseManager
from garmin.io.file_manager import FileManager

def main():
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

if __name__ == "__main__":
    main()