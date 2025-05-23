## garmin/api.py
import os
import garth
from getpass import getpass
from garth.exc import GarthException
import json
import boto3
from botocore.exceptions import ClientError
import tempfile
import shutil

class GarminSession:
    """
    Wraps garth.client for login/session management.
    """
    def __init__(self, data_dir=None, garth_home=None):
        # Set default data directory relative to this file if not provided.
        if data_dir is None:
            data_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '../data'))
        self.data_dir = data_dir
        # Set garth_home relative to data_dir if not provided.
        if garth_home is None:
            self.garth_home = os.path.join(self.data_dir, 'sessions', 'garth')
        else:
            self.garth_home = garth_home

        self.username = os.environ.get('GARMIN_USERNAME')
        self.password = os.environ.get('GARMIN_PASSWORD')
        self.garth = None
        self._connected = False

    def _is_aws(self):
        return os.environ.get('AWS_EXECUTION_ENV') is not None or os.environ.get('GARMIN_USE_AWS_SECRETS') == '1'

    def _get_secret_name(self):
        # You may want to customize this per user/environment
        return os.environ.get('GARMIN_AWS_SECRET_NAME', 'garmin/oauth2_token')

    def _load_token(self):
        if self._is_aws():
            secret_name2 = self._get_secret_name()  # garmin/oauth2_token
            secret_name1 = secret_name2.replace('oauth2_token', 'oauth1_token')
            region = os.environ.get('AWS_REGION', 'us-east-2')
            print(f"[DEBUG] Loading token from AWS Secrets Manager: {secret_name2} and {secret_name1} in region {region}")
            client = boto3.client('secretsmanager', region_name=region)
            temp_dir = tempfile.mkdtemp()
            # Load oauth2_token.json
            try:
                get_secret_value_response = client.get_secret_value(SecretId=secret_name2)
                secret2 = get_secret_value_response['SecretString']
                print(f"[DEBUG] oauth2_token secret loaded, length: {len(secret2)}")
                token_data2 = json.loads(secret2)
                oauth2_path = os.path.join(temp_dir, 'oauth2_token.json')
                with open(oauth2_path, 'w') as f:
                    json.dump(token_data2, f)
                print(f"[DEBUG] Written oauth2_token.json to temp dir: {oauth2_path}")
            except ClientError as e:
                print(f"Error loading oauth2_token from AWS Secrets Manager: {e}")
                raise
            # Load oauth1_token.json (optional)
            try:
                get_secret_value_response = client.get_secret_value(SecretId=secret_name1)
                secret1 = get_secret_value_response['SecretString']
                print(f"[DEBUG] oauth1_token secret loaded, length: {len(secret1)}")
                token_data1 = json.loads(secret1)
                oauth1_path = os.path.join(temp_dir, 'oauth1_token.json')
                with open(oauth1_path, 'w') as f:
                    json.dump(token_data1, f)
                print(f"[DEBUG] Written oauth1_token.json to temp dir: {oauth1_path}")
            except ClientError as e:
                print(f"[DEBUG] oauth1_token not found or error: {e}. Writing empty oauth1_token.json.")
                oauth1_path = os.path.join(temp_dir, 'oauth1_token.json')
                with open(oauth1_path, 'w') as f:
                    json.dump({}, f)
            garth.resume(temp_dir)
        else:
            print(f"[DEBUG] Loading token from local file: {self.garth_home}")
            garth.resume(self.garth_home)

    def _save_token(self):
        if self._is_aws():
            secret_name = self._get_secret_name()
            region = os.environ.get('AWS_REGION', 'us-east-2')
            print(f"[DEBUG] Saving token to AWS Secrets Manager: {secret_name} in region {region}")
            client = boto3.client('secretsmanager', region_name=region)
            # Read the token file
            with open(self.garth_home, 'r') as f:
                token_data = f.read()
            try:
                client.put_secret_value(SecretId=secret_name, SecretString=token_data)
                print(f"[DEBUG] Token saved to AWS Secrets Manager.")
            except ClientError as e:
                print(f"Error saving token to AWS Secrets Manager: {e}")
                raise
        else:
            print(f"[DEBUG] Saving token to local file: {self.garth_home}")
            garth.save(self.garth_home)

    def connect(self):
        """
        Connects to Garmin Connect via the garth package. Uses a stored session if available.
        """
        try:
            self._load_token()
            _ = garth.client.username  # Test if already logged in.
        except (FileNotFoundError, GarthException, KeyError, AttributeError):
            if not self.username or not self.password:
                print("Environment variables GARMIN_USERNAME and/or GARMIN_PASSWORD not set.")
                self.username = input("Email: ")
                self.password = getpass("Password: ")
            garth.client.login(self.username, self.password)
            self._save_token()
        self.garth = garth
        self._connected = True

    def refresh_token(self):
        """
        Forces a token refresh and updates the token in AWS/local file.
        """
        if not self._connected:
            self.connect()
        # Make a trivial authenticated request to force refresh if needed
        _ = self.garth.client.username
        self._save_token()

    def get(self, url):
        if not self._connected:
            self.connect()
        return self.garth.client.connectapi(url)

    def post(self, url):
        if not self._connected:
            self.connect()
        return self.garth.client.connectapi(url, method='POST')