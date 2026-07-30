from __future__ import annotations

from pathlib import Path

from garmin.analysis.plotting import make_metric_bokeh_plot
from garmin.data_processor.processor import GarminDataProcessor
from garmin.io.curated_store import CuratedDataStore
from garmin.io.file_manager import FileManager
from garmin.scripts.manual_process_data import load_curated_daily_inputs


CURATED_BOKEH_METRICS = ["health_stats", "heart_rate", "sleep", "steps"]


def curated_dashboard_relative_dir(source: str) -> str:
    return f"dashboards/curated_metric_timeseries/{source}"


def _artifact_file_pairs(relative_dir: str) -> list[tuple[str, str]]:
    return [
        (f"{relative_dir}/{metric}_timeseries_script.html", f"{relative_dir}/{metric}_timeseries_div.html")
        for metric in CURATED_BOKEH_METRICS
    ]


def build_curated_dashboard_artifacts(source: str = "local") -> str:
    if source not in {"local", "s3"}:
        raise ValueError(f"Unsupported dashboard source: {source}")

    processor = GarminDataProcessor()
    read_manager = FileManager(environment="aws" if source == "s3" else "local")
    write_manager = FileManager(environment="aws" if source == "s3" else "local")
    curated_store = CuratedDataStore(file_manager=read_manager)

    raw_inputs = load_curated_daily_inputs(curated_store)
    processed_data = processor.process_all(raw_inputs)
    moving_averages = processor.calculate_moving_averages_all(
        processed_data,
        kernels=["gaussian", "boxcar"],
        bandwidths=[1] + list(range(7, 150, 7)),
    )

    relative_dir = curated_dashboard_relative_dir(source)
    if source == "local":
        output_dir = Path(write_manager.local_dir) / relative_dir
        output_dir.mkdir(parents=True, exist_ok=True)
    else:
        output_dir = f"s3://{write_manager.s3_bucket}/{relative_dir}"

    for metric in CURATED_BOKEH_METRICS:
        script, div = make_metric_bokeh_plot(
            metric,
            processed_data[metric],
            moving_averages[metric],
            ma_lims=(0, 150),
        )
        write_manager.write_text(script, f"{relative_dir}/{metric}_timeseries_script.html")
        write_manager.write_text(div, f"{relative_dir}/{metric}_timeseries_div.html")

    return str(output_dir)


def cache_curated_dashboard_artifacts_locally(source: str = "s3") -> Path:
    if source not in {"local", "s3"}:
        raise ValueError(f"Unsupported dashboard source: {source}")

    relative_dir = curated_dashboard_relative_dir(source)
    local_manager = FileManager(environment="local")
    cache_dir = Path(local_manager.local_dir) / relative_dir
    cache_dir.mkdir(parents=True, exist_ok=True)

    if source == "local":
        return cache_dir

    s3_manager = FileManager(environment="aws")
    for script_path, div_path in _artifact_file_pairs(relative_dir):
        local_manager.write_text(s3_manager.read_text(script_path), script_path)
        local_manager.write_text(s3_manager.read_text(div_path), div_path)

    return cache_dir