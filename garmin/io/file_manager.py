import os
import pandas as pd
import boto3

class FileManager:
    """
    General file manager for reading/writing data files locally or to S3, depending on environment.
    """
    def __init__(self, environment=None, local_dir=None, s3_bucket=None, s3_prefix=None):
        if environment is None:
            if 'AWS_EXECUTION_ENV' in os.environ:
                environment = 'aws'
            else:
                environment = 'local'
        self.environment = environment
        self.local_dir = local_dir or os.path.abspath(os.path.join(os.path.dirname(__file__), '../../data'))
        self.s3_bucket = s3_bucket or os.environ.get('GARMIN_S3_BUCKET')
        self.s3_prefix = s3_prefix or ''
        if self.environment == 'aws' and boto3 is None:
            raise ImportError("boto3 is required for AWS S3 operations.")

    def _local_path(self, filename):
        return os.path.join(self.local_dir, filename)

    def _s3_key(self, filename):
        return f"{self.s3_prefix}{filename}"

    def write_df(self, df, filename, format='parquet'):
        if self.environment == 'aws':
            self._write_df_s3(df, filename, format)
        else:
            os.makedirs(self.local_dir, exist_ok=True)
            if format == 'parquet':
                df.to_parquet(self._local_path(filename), index=False)
            elif format == 'csv':
                df.to_csv(self._local_path(filename), index=False)
            else:
                raise ValueError(f"Unsupported format: {format}")

    def read_df(self, filename, format='parquet'):
        if self.environment == 'aws':
            return self._read_df_s3(filename, format)
        else:
            if format == 'parquet':
                return pd.read_parquet(self._local_path(filename))
            elif format == 'csv':
                return pd.read_csv(self._local_path(filename))
            else:
                raise ValueError(f"Unsupported format: {format}")

    def _write_df_s3(self, df, filename, format):
        import io
        buffer = io.BytesIO()
        if format == 'parquet':
            df.to_parquet(buffer, index=False)
        elif format == 'csv':
            buffer = io.StringIO()
            df.to_csv(buffer, index=False)
            buffer.seek(0)
        else:
            raise ValueError(f"Unsupported format: {format}")
        buffer.seek(0)
        s3 = boto3.client('s3')
        s3.upload_fileobj(buffer, self.s3_bucket, self._s3_key(filename))

    def _read_df_s3(self, filename, format):
        import io
        s3 = boto3.client('s3')
        buffer = io.BytesIO()
        s3.download_fileobj(self.s3_bucket, self._s3_key(filename), buffer)
        buffer.seek(0)
        if format == 'parquet':
            return pd.read_parquet(buffer)
        elif format == 'csv':
            buffer = io.StringIO(buffer.read().decode())
            return pd.read_csv(buffer)
        else:
            raise ValueError(f"Unsupported format: {format}")
