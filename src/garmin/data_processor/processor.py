# garmin/data_processor/processor.py

import numpy as np
import pandas as pd
from myutils.utils import kernel_smooth_with_uncertainty


class GarminDataProcessor:
    """
    Provides functions to process and analyze Garmin data.
    """
    MOVING_AVERAGE_COLUMNS = {
        'health_stats': ['weight', 'bmi', 'body_fat', 'body_water', 'bone_mass', 'muscle_mass', 'fat_mass'],
        'sleep': ['resting_hr', 'sleep_score', 'total_sleep_time', 'rem_time', 'deep_time', 'light_time', 'awake_time', 'sleep_start_time', 'sleep_end_time', 'hrv_7d_average'],
        'steps': ['step_goal', 'total_steps', 'total_distance'],
        'stress': ['high_stress_duration', 'low_stress_duration', 'overall_stress_level', 'rest_stress_duration'],
        'heart_rate': ['resting_hr', 'wellness_max_avg_hr', 'wellness_min_avg_hr'],
        'body_battery': ['low_body_battery', 'high_body_battery'],
    }

    def process_health_stats(self, df):
        """
        Processes health stats DataFrame to compute additional metrics.
        """
        df = df.copy()
        df = df.drop(columns=['id', 'date_pulled'])
        df['date'] = pd.to_datetime(df['date'])
        for col in ['body_fat', 'bmi', 'fat_mass']:
            df.loc[df[col] == 0, col] = np.nan
        return df
    
    def process_sleep(self, df):
        """
        Processes sleep stats DataFrame to compute additional metrics.
        """
        df = df.copy()
        df = df.drop(columns=['id', 'date_pulled'])
        df['date'] = pd.to_datetime(df['date'])
        for col in ['total_sleep_time', 'rem_time', 'deep_time', 'light_time', 'awake_time']:
            df[col] /= 60 * 60
        df['sleep_start_time'] = pd.to_datetime(df['local_sleep_start_time'], unit='ms') - df['date'] + pd.Timedelta(days=1)
        df['sleep_start_time'] = df['sleep_start_time'].dt.total_seconds() / 3600

        df['sleep_end_time'] = pd.to_datetime(df['local_sleep_time_end'], unit='ms') - df['date']
        df['sleep_end_time'] = df['sleep_end_time'].dt.total_seconds() / 3600

        return_cols = [
            'date',
            'resting_hr',
            'sleep_score',
            'total_sleep_time',
            'rem_time',
            'deep_time',
            'light_time',
            'awake_time',
            'sleep_start_time',
            'sleep_end_time',
            'hrv_7d_average',
        ]
        return df[return_cols]
    
    def process_steps(self, df):
        """
        Processes steps DataFrame to compute additional metrics.
        """
        df = df.copy()
        df = df.drop(columns=['id', 'date_pulled'])
        df['date'] = pd.to_datetime(df['date'])
        return df
    
    def process_stress(self, df):
        """
        Processes steps DataFrame to compute additional metrics.
        """
        df = df.copy()
        df = df.drop(columns=['id', 'date_pulled'])
        df['date'] = pd.to_datetime(df['date'])
        for col in ['high_stress_duration', 'low_stress_duration', 'rest_stress_duration']:
            df[col] /= 3600.
        return df

    def process_heart_rate(self, df):
        """
        Processes heart rate DataFrame to compute additional metrics.
        """
        df = df.copy()
        df = df.drop(columns=['id', 'date_pulled'])
        df['date'] = pd.to_datetime(df['date'])
        return df
    
    def process_body_battery(self, df):
        """
        Processes body battery DataFrame to compute additional metrics.
        """
        df = df.copy()
        df = df.drop(columns=['id', 'date_pulled'])
        df['date'] = pd.to_datetime(df['date'])
        return df

    def process_all(self, df_dict):
        processed = {}
        processed['health_stats'] = self.process_health_stats(df_dict['health_stats'])
        processed['sleep'] = self.process_sleep(df_dict['sleep'])
        processed['steps'] = self.process_steps(df_dict['steps'])
        processed['stress'] = self.process_stress(df_dict['stress'])
        processed['heart_rate'] = self.process_heart_rate(df_dict['heart_rate'])
        processed['body_battery'] = self.process_body_battery(df_dict['body_battery'])
        return processed
    
    def calculate_moving_averages(
        self,
        df,
        columns,
        kernels,
        bandwidths,
    ):
        """
        Calculates moving averages for specified columns in the DataFrame using Gaussian kernels.
        
        Args:
            df (pd.DataFrame): Input DataFrame.
            columns (list): List of column names to calculate moving averages for.
            kernels (list): List of kernel types to use ('simple', 'gaussian').
            bandwidths (list): List of bandwidths for Gaussian smoothing.
        
        Returns:
            pd.DataFrame: DataFrame with new columns for moving averages.
        """
        df_out = df[['date']].copy()
        x_out = df['date'].values
        for col in columns:
            for kernel in kernels:
                for bw in bandwidths:
                    smoothed, _ = kernel_smooth_with_uncertainty(
                        df,
                        'date',
                        col,
                        yerr_col=None,
                        kernel=kernel,
                        bandwidth=bw,
                        x_out=x_out
                    )
                    df_out[f"{col}_{kernel}_{bw}"] = smoothed
        return df_out
    
    def calculate_moving_averages_all(self, df_dict, kernels, bandwidths):
        results = {}
        for key, df in df_dict.items():
            print(f"Calculating moving averages for {key} with kernels {kernels} and bandwidths {bandwidths}")
            columns = self.MOVING_AVERAGE_COLUMNS.get(key)
            if columns is not None:
                results[key] = self.calculate_moving_averages(df, columns, kernels, bandwidths)
        return results

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
