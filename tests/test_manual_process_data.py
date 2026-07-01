from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from garmin.data_processor.processor import GarminDataProcessor
from garmin.io.curated_store import CuratedDataStore
from garmin.io.file_manager import FileManager
from garmin.scripts.manual_process_data import DAILY_DATASETS, load_curated_daily_inputs


def _build_daily_dataset(dataset: str) -> pd.DataFrame:
    base = {
        'date': [date(2024, 1, 1)],
        'date_pulled': [date(2024, 1, 2)],
    }
    if dataset == 'health_stats':
        return pd.DataFrame(base | {
            'weight': [180.0],
            'bmi': [25.0],
            'body_fat': [15.0],
            'body_water': [55.0],
            'bone_mass': [7.0],
            'muscle_mass': [80.0],
            'fat_mass': [27.0],
        })
    if dataset == 'sleep':
        return pd.DataFrame(base | {
            'rem_time': [7200.0],
            'resting_hr': [50.0],
            'local_sleep_start_time': [1704153600000.0],
            'local_sleep_time_end': [1704182400000.0],
            'gmt_sleep_start_time': [1704153600000.0],
            'gmt_sleep_end_time': [1704182400000.0],
            'total_sleep_time': [28800.0],
            'deep_time': [5400.0],
            'awake_time': [1200.0],
            'light_time': [14400.0],
            'sleep_score_quality': ['GOOD'],
            'respiration': [12.0],
            'spo2': [98.0],
            'hrv_status': ['BALANCED'],
            'sleep_need': [8.0],
            'body_battery_change': [20.0],
            'skin_temp_f': [98.6],
            'skin_temp_c': [37.0],
            'hrv_7d_average': [45.0],
            'sleep_score': [85.0],
        })
    if dataset == 'steps':
        return pd.DataFrame(base | {
            'step_goal': [10000.0],
            'total_steps': [9500.0],
            'total_distance': [5.0],
        })
    if dataset == 'stress':
        return pd.DataFrame(base | {
            'high_stress_duration': [3600.0],
            'low_stress_duration': [7200.0],
            'overall_stress_level': [20.0],
            'rest_stress_duration': [1800.0],
        })
    if dataset == 'heart_rate':
        return pd.DataFrame(base | {
            'resting_hr': [50.0],
            'wellness_max_avg_hr': [130.0],
            'wellness_min_avg_hr': [45.0],
        })
    if dataset == 'body_battery':
        return pd.DataFrame(base | {
            'low_body_battery': [35.0],
            'high_body_battery': [90.0],
        })
    raise AssertionError(f'Unhandled dataset {dataset}')


def test_load_curated_daily_inputs_reads_all_required_datasets(tmp_path) -> None:
    store = CuratedDataStore(
        file_manager=FileManager(environment='local', local_dir=str(tmp_path))
    )
    for dataset in DAILY_DATASETS:
        store.merge_daily(dataset, _build_daily_dataset(dataset))

    loaded = load_curated_daily_inputs(store)

    assert set(loaded) == set(DAILY_DATASETS)
    assert loaded['steps'].iloc[0]['total_steps'] == 9500.0


def test_load_curated_daily_inputs_raises_when_any_dataset_is_missing(tmp_path) -> None:
    store = CuratedDataStore(
        file_manager=FileManager(environment='local', local_dir=str(tmp_path))
    )
    for dataset in DAILY_DATASETS[:-1]:
        store.merge_daily(dataset, _build_daily_dataset(dataset))

    with pytest.raises(ValueError, match='Missing curated daily datasets'):
        load_curated_daily_inputs(store)


def test_processor_accepts_curated_inputs_without_sql_id_columns(tmp_path) -> None:
    store = CuratedDataStore(
        file_manager=FileManager(environment='local', local_dir=str(tmp_path))
    )
    for dataset in DAILY_DATASETS:
        store.merge_daily(dataset, _build_daily_dataset(dataset))

    raw_inputs = load_curated_daily_inputs(store)
    processed = GarminDataProcessor().process_all(raw_inputs)

    assert set(processed) == {
        'health_stats',
        'sleep',
        'steps',
        'stress',
        'heart_rate',
        'body_battery',
    }
    assert 'id' not in processed['health_stats'].columns