# garmin/data_processor/processor.py

import numpy as np
import pandas as pd

class GarminDataProcessor:
    """
    Provides functions to process and analyze Garmin data.
    """
    @staticmethod
    def smooth_series(s, window, std):
        """
        Applies a Gaussian-weighted rolling average to a pandas Series.
        """
        def gaussian_avg(x):
            n = len(x)
            weights = np.exp(-((np.arange(n) - (n - 1) / 2) ** 2) / (2 * std**2))
            mask = x != -1  # Only consider valid (non -1) values
            if mask.sum() == 0:
                return np.nan
            weights = weights[mask]
            weights = weights / weights.sum()
            return (x[mask].values * weights).sum()
        return s.fillna(-1).rolling(window, center=True).apply(gaussian_avg, raw=False)

    @staticmethod
    def add_weight_smoothing(df):
        """
        Adds various smoothed versions of weight and composition columns to the DataFrame.
        """
        for col in ['Weight', 'FatMass', 'MuscleMass', 'BodyWater', 'BoneMass']:
            df[f'{col}_smoothed'] = GarminDataProcessor.smooth_series(df[col], window=21, std=5)
            df[f'{col}_smoothed_shortterm'] = GarminDataProcessor.smooth_series(df[col], window=13, std=3)
            df[f'{col}_smoothed_veryshortterm'] = GarminDataProcessor.smooth_series(df[col], window=5, std=3)
            df[f'{col}_smoothed_longterm'] = GarminDataProcessor.smooth_series(df[col], window=101, std=28)
        for suffix in ['', '_smoothed', '_smoothed_shortterm', '_smoothed_veryshortterm', '_smoothed_longterm']:
            df[f'BonePlusWater{suffix}'] = df[f'BodyWater{suffix}'] + df[f'BoneMass{suffix}']
        return df

    @staticmethod
    def analyze_workout(df):
        """
        Analyzes a strength workout DataFrame to compute aggregated metrics.
        """
        df = df.copy()
        df['CorrectedWeight'] = df.apply(
            lambda row: 2. * row['Weight'] if (row['Exercise'] == 'BENCH_PRESS') and (row['Weight'] < 66) else row['Weight'],
            axis=1
        )
        df['1RMConv'] = df['CorrectedWeight'] / (1.0278 - 0.0278 * df['Reps'])
        df['WeightReps'] = df['CorrectedWeight'] * df['Reps']
        agg_df = df.groupby('Exercise', as_index=False).agg(
            MaxWeight=('CorrectedWeight', 'max'),
            TotalReps=('Reps', 'sum'),
            AvgReps=('Reps', 'mean'),
            TotalWeightReps=('WeightReps', 'sum'),
            Max1RM=('1RMConv', 'max')
        )
        agg_df['AvgWeightPerRep'] = agg_df['TotalWeightReps'] / agg_df['TotalReps']
        agg_df['Avg1RM'] = agg_df['AvgWeightPerRep'] / (1.0278 - 0.0278 * agg_df['AvgReps'])
        return agg_df
