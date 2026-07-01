"""High-level Garmin session boundary used by pullers and scripts."""

from __future__ import annotations

import os
from getpass import getpass

from .client import GarminConnectClient
from .exceptions import (
    GarminAuthenticationError,
    GarminLoginFlowNotImplementedError,
    GarminRequestError,
)
from .storage import TokenStore, TokenStoreConfig
from .tokens import GarminAuthState, OAuth2Token


class GarminSession:
    """Owns Garmin auth state and authenticated Connect API access."""

    def __init__(
        self,
        data_dir: str | None = None,
        session_dir: str | None = None,
        garth_home: str | None = None,
        *,
        domain: str = "garmin.com",
    ) -> None:
        if data_dir is None:
            data_dir = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "../../data")
            )
        self.data_dir = data_dir
        self.session_dir = session_dir or garth_home or os.path.join(
            self.data_dir, "sessions", "garmin_connect"
        )
        legacy_session_dir = os.path.join(self.data_dir, "sessions", "garth")
        if os.path.abspath(legacy_session_dir) == os.path.abspath(self.session_dir):
            legacy_session_dir = None
        self.legacy_session_dir = legacy_session_dir

        self.username = os.environ.get("GARMIN_USERNAME")
        self.password = os.environ.get("GARMIN_PASSWORD")
        self._connected = False
        self._auth_state: GarminAuthState | None = None

        self._token_store = TokenStore(
            TokenStoreConfig(
                session_dir=self.session_dir,
                legacy_session_dir=self.legacy_session_dir,
                use_aws_secrets=self._is_aws(),
                secret_name=self._get_secret_name(),
                region=os.environ.get("AWS_REGION", "us-east-2"),
            )
        )
        self._client = GarminConnectClient(
            domain=domain,
            refresh_callback=self.refresh_token,
        )

    def _is_aws(self) -> bool:
        return os.environ.get("AWS_EXECUTION_ENV") is not None or os.environ.get(
            "GARMIN_USE_AWS_SECRETS"
        ) == "1"

    def _get_secret_name(self) -> str:
        return os.environ.get("GARMIN_AWS_SECRET_NAME", "garmin/oauth2_token")

    def _load_token(self) -> GarminAuthState:
        return self._token_store.load()

    def _save_token(self) -> None:
        if self._auth_state is None:
            raise GarminAuthenticationError("No Garmin auth state is available to save.")
        self._token_store.save(self._auth_state)

    def _apply_auth_state(self, auth_state: GarminAuthState) -> None:
        self._auth_state = auth_state
        self._client.set_oauth2_token(auth_state.oauth2_token)

    def _prompt_for_credentials(self) -> None:
        if not self.username:
            print("Environment variable GARMIN_USERNAME not set.")
            self.username = input("Email: ")
        if not self.password:
            self.password = getpass("Password: ")

    def _login(self) -> None:
        self._prompt_for_credentials()
        raise GarminLoginFlowNotImplementedError(
            "Interactive Garmin login is not implemented in garmin.api yet. "
            "Load a valid oauth2 token from GARMIN_OAUTH2_JSON, GARMIN_ACCESS_TOKEN, "
            f"or {os.path.join(self.session_dir, 'oauth2_token.json')} while the new login flow is built."
        )

    def _validate_session(self) -> None:
        profile = self._client.get("/userprofile-service/socialProfile")
        if isinstance(profile, dict) and profile.get("userName"):
            self.username = profile["userName"]

    def connect(self) -> "GarminSession":
        try:
            self._apply_auth_state(self._load_token())
            self._validate_session()
        except (FileNotFoundError, GarminAuthenticationError, GarminRequestError, ValueError):
            self._login()
        self._connected = True
        return self

    def refresh_token(self) -> OAuth2Token:
        if self._auth_state is None or self._auth_state.oauth1_token is None:
            raise GarminLoginFlowNotImplementedError(
                "OAuth token refresh is not implemented yet in the repo-owned Garmin client. "
                "The next step is to replace Garmin's login and token exchange flow in garmin.api."
            )
        raise GarminLoginFlowNotImplementedError(
            "Stored oauth1 state is present, but the repo does not yet implement Garmin's current token refresh flow."
        )

    def get(self, url: str):
        if not self._connected:
            self.connect()
        return self._client.get(url)

    def post(self, url: str):
        if not self._connected:
            self.connect()
        return self._client.post(url)