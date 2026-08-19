"""Tests for the dataset-name to Garmin-typeKey mapping.

This mapping exists because the registry used to derive the typeKey from the
dataset name. That silently broke `tennis`: Garmin calls it `tennis_v2`, so
the dataset was registered, queried, and came back empty for years -- which
looks exactly like "never played tennis". Nothing errored, nothing logged.

Garmin has versioned several keys with a `_v2` suffix, so this is a category
of mismatch, not a one-off. These tests pin the mapping and, more importantly,
pin that the registry actually consults it.
"""

from __future__ import annotations

import pandas as pd
import pytest

from garmin.datasets import ACTIVITY_DATASETS, ACTIVITY_TYPE_KEYS, activity_type_key
from garmin.updaters import DataUpdater


@pytest.mark.parametrize(('dataset', 'expected'), [
    ('tennis', 'tennis_v2'),
    ('paddling', 'paddling_v2'),
    ('transition', 'transition_v2'),
    ('skiing', 'resort_skiing_snowboarding_ws'),
    ('strength', 'strength_training'),
])
def test_datasets_whose_name_differs_from_garmins_key(dataset, expected):
    assert activity_type_key(dataset) == expected


@pytest.mark.parametrize('dataset', ['running', 'cycling', 'bouldering', 'indoor_climbing'])
def test_datasets_whose_name_matches_garmins_key(dataset):
    assert activity_type_key(dataset) == dataset


def test_every_override_names_a_real_dataset():
    """An override for a dataset that doesn't exist is dead config that would
    quietly never apply."""
    assert set(ACTIVITY_TYPE_KEYS) <= set(ACTIVITY_DATASETS)


def test_type_keys_are_unique_across_datasets():
    """Two datasets claiming one typeKey would each pull the same activities."""
    keys = [activity_type_key(d) for d in ACTIVITY_DATASETS]
    assert len(keys) == len(set(keys))


def _registry(monkeypatch):
    updater = DataUpdater(
        session=object(), curated_store=object(), health_puller=object(),
        health_detailed_puller=object(), activity_puller=_StubPuller(),
        training_puller=object(),
    )
    return updater._activity_type_registry()


class _StubPuller:
    def __init__(self):
        self.cardio_calls = []

    def pull_cardio_summary(self, activity_type, start, end):
        self.cardio_calls.append(activity_type)
        return pd.DataFrame()

    def pull_running_summary(self, start, end):
        return pd.DataFrame()

    def pull_strength_summary(self, start, end):
        return pd.DataFrame()

    def get_activity_detail_timeseries(self, activity_id):
        return pd.DataFrame()

    def get_strength_workout(self, activity_id):
        return pd.DataFrame()


def test_registry_uses_the_mapped_key_not_the_dataset_name(monkeypatch):
    """The actual regression: the registry must ask Garmin for `tennis_v2`."""
    entries = _registry(monkeypatch)
    by_dataset = {e['dataset']: e['activity_type'] for e in entries}
    assert by_dataset['tennis'] == 'tennis_v2'
    assert by_dataset['skiing'] == 'resort_skiing_snowboarding_ws'
    assert by_dataset['running'] == 'running'


def test_registry_summary_fn_requests_the_mapped_key(monkeypatch):
    """Binding the loop variable wrongly here would send every sport the same
    typeKey -- so assert the call Garmin would actually receive."""
    puller = _StubPuller()
    updater = DataUpdater(
        session=object(), curated_store=object(), health_puller=object(),
        health_detailed_puller=object(), activity_puller=puller,
        training_puller=object(),
    )
    entries = {e['dataset']: e for e in updater._activity_type_registry()}
    entries['tennis']['summary_fn']('2026-01-01', '2026-01-02')
    entries['paddling']['summary_fn']('2026-01-01', '2026-01-02')
    assert puller.cardio_calls == ['tennis_v2', 'paddling_v2']


def test_the_previously_discarded_sports_are_registered():
    """280 activities across these types were pulled and dropped before
    2026-08-19."""
    recovered = {
        'treadmill_running', 'indoor_running', 'trail_running',
        'indoor_climbing', 'swimming', 'walking', 'volleyball', 'paddling',
        'softball', 'skiing', 'fitness_equipment', 'other',
        'multi_sport', 'transition',
    }
    assert recovered <= set(ACTIVITY_DATASETS)


def test_every_dataset_gets_a_registry_entry():
    updater = DataUpdater(
        session=object(), curated_store=object(), health_puller=object(),
        health_detailed_puller=object(), activity_puller=_StubPuller(),
        training_puller=object(),
    )
    entries = updater._activity_type_registry()
    assert {e['dataset'] for e in entries} == set(ACTIVITY_DATASETS)
