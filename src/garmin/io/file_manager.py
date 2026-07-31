import os
import pandas as pd
import boto3

from garmin._paths import get_data_dir


def _resolve_runtime_environment() -> str:
    """Resolve whether file IO should use local disk or AWS resources."""

    explicit_env = os.environ.get('GARMIN_RUNTIME_ENV')
    if explicit_env in {'local', 'aws'}:
        return explicit_env
    return 'aws' if (
        os.environ.get('AWS_EXECUTION_ENV') is not None
        and os.environ.get('LAMBDA_TASK_ROOT') is not None
    ) else 'local'

class FileManager:
    """
    General file manager for reading/writing data files locally or to S3, depending on environment.
    """
    def __init__(self, environment=None, local_dir=None, s3_bucket=None, s3_prefix=None):
        if environment is None:
            environment = _resolve_runtime_environment()
        self.environment = environment
        self.local_dir = local_dir or str(get_data_dir())
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
            local_path = self._local_path(filename)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            if format == 'parquet':
                df.to_parquet(local_path, index=False)
            elif format == 'csv':
                df.to_csv(local_path, index=False)
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

    def list_files(self, prefix):
        """List file paths (relative to the data root) under a prefix, local or S3."""
        if self.environment == 'aws':
            return self._list_files_s3(prefix)
        return self._list_files_local(prefix)

    def _list_files_local(self, prefix):
        base = self._local_path(prefix)
        if not os.path.isdir(base):
            return []
        results = []
        for root, _, files in os.walk(base):
            for fname in files:
                full = os.path.join(root, fname)
                rel = os.path.relpath(full, self.local_dir)
                results.append(rel.replace(os.sep, '/'))
        return sorted(results)

    def _list_files_s3(self, prefix):
        s3 = boto3.client('s3')
        key_prefix = self._s3_key(prefix)
        paginator = s3.get_paginator('list_objects_v2')
        results = []
        for page in paginator.paginate(Bucket=self.s3_bucket, Prefix=key_prefix):
            for obj in page.get('Contents', []):
                key = obj['Key']
                if self.s3_prefix and key.startswith(self.s3_prefix):
                    key = key[len(self.s3_prefix):]
                results.append(key)
        return sorted(results)

    def write_text(self, text, filename):
        """Write a string to a file (local or S3)."""
        if self.environment == 'aws':
            import io
            s3 = boto3.client('s3')
            buffer = io.BytesIO(text.encode('utf-8'))
            s3.upload_fileobj(buffer, self.s3_bucket, self._s3_key(filename))
        else:
            os.makedirs(os.path.dirname(self._local_path(filename)), exist_ok=True)
            with open(self._local_path(filename), 'w', encoding='utf-8') as f:
                f.write(text)

    def read_text(self, filename):
        """Read a string from a file (local or S3)."""
        if self.environment == 'aws':
            import io
            s3 = boto3.client('s3')
            buffer = io.BytesIO()
            s3.download_fileobj(self.s3_bucket, self._s3_key(filename), buffer)
            buffer.seek(0)
            return buffer.read().decode('utf-8')
        else:
            with open(self._local_path(filename), 'r', encoding='utf-8') as f:
                return f.read()
