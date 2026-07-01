from dotenv import load_dotenv
load_dotenv()

import argparse

from garmin.data_processor.processor import GarminDataProcessor
from garmin.io.curated_store import CuratedDataStore
from garmin.io.file_manager import FileManager


DAILY_DATASETS = [
    'health_stats',
    'sleep',
    'steps',
    'stress',
    'heart_rate',
    'body_battery',
]


def load_curated_daily_inputs(curated_store: CuratedDataStore) -> dict[str, object]:
    raw_data_dict = {dataset: curated_store.load_daily(dataset) for dataset in DAILY_DATASETS}
    missing = [dataset for dataset, df in raw_data_dict.items() if df.empty]
    if missing:
        raise ValueError(
            'Missing curated daily datasets: '
            + ', '.join(missing)
            + '. Run garmin.scripts.manual_update first to populate curated storage.'
        )
    return raw_data_dict


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Process curated Garmin parquet outputs.")
    parser.add_argument(
        "--storage-target",
        choices=["local", "s3"],
        default="local",
        help="Where curated inputs are read from and processed outputs are written.",
    )
    return parser


def main(argv: list[str] | None = None):
    args = build_parser().parse_args(argv)
    proc = GarminDataProcessor()
    fm = FileManager(environment="aws" if args.storage_target == "s3" else "local")
    curated_store = CuratedDataStore(file_manager=fm)

    raw_data_dict = load_curated_daily_inputs(curated_store)
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