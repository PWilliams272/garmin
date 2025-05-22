## garmin/api.py
import os
import garth
from getpass import getpass
from garth.exc import GarthException

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

    def connect(self):
        """
        Connects to Garmin Connect via the garth package. Uses a stored session if available.
        """
        try:
            garth.resume(self.garth_home)
            _ = garth.client.username  # Test if already logged in.
        except (FileNotFoundError, GarthException):
            if not self.username or not self.password:
                print("Environment variables GARMIN_USERNAME and/or GARMIN_PASSWORD not set.")
                self.username = input("Email: ")
                self.password = getpass("Password: ")
            garth.client.login(self.username, self.password)
            garth.save(self.garth_home)
        self.garth = garth
        self._connected = True

    def get(self, url):
        if not self._connected:
            self.connect()
        return self.garth.client.connectapi(url)

    def post(self, url):
        if not self._connected:
            self.connect()
        return self.garth.client.connectapi(url, method='POST')