"""Token persistence for local development and AWS execution."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

from .exceptions import GarminTokenStoreError
from .tokens import GarminAuthState, OAuth1Token, OAuth2Token


@dataclass(slots=True)
class TokenStoreConfig:
    """Configuration for loading and saving Garmin auth state."""

    session_dir: str
    legacy_session_dir: str | None = None
    use_aws_secrets: bool = False
    secret_name: str = "garmin/oauth2_token"
    region: str = "us-east-2"


class TokenStore:
    """Loads and saves Garmin auth state from env vars, disk, or AWS secrets."""

    def __init__(self, config: TokenStoreConfig) -> None:
        self.config = config

    def load(self) -> GarminAuthState:
        env_state = self._load_from_env()
        if env_state is not None:
            return env_state
        if self.config.use_aws_secrets:
            return self._load_from_aws()
        return self._load_from_local()

    def save(self, auth_state: GarminAuthState) -> None:
        if self.config.use_aws_secrets:
            self._save_to_aws(auth_state)
            return
        self._save_to_local(auth_state)

    def _load_from_env(self) -> GarminAuthState | None:
        oauth2_json = os.environ.get("GARMIN_OAUTH2_JSON")
        if oauth2_json:
            oauth2_token = OAuth2Token.from_dict(json.loads(oauth2_json))
            return GarminAuthState(oauth2_token=oauth2_token)

        access_token = os.environ.get("GARMIN_ACCESS_TOKEN")
        if not access_token:
            return None

        raw_token: dict[str, Any] = {
            "access_token": access_token,
            "token_type": os.environ.get("GARMIN_TOKEN_TYPE", "Bearer"),
        }
        if os.environ.get("GARMIN_REFRESH_TOKEN"):
            raw_token["refresh_token"] = os.environ["GARMIN_REFRESH_TOKEN"]
        if os.environ.get("GARMIN_EXPIRES_AT"):
            raw_token["expires_at"] = int(os.environ["GARMIN_EXPIRES_AT"])
        if os.environ.get("GARMIN_REFRESH_EXPIRES_AT"):
            raw_token["refresh_token_expires_at"] = int(
                os.environ["GARMIN_REFRESH_EXPIRES_AT"]
            )
        return GarminAuthState(oauth2_token=OAuth2Token.from_dict(raw_token))

    def _candidate_dirs(self) -> list[Path]:
        candidates = [Path(self.config.session_dir)]
        if self.config.legacy_session_dir:
            legacy_dir = Path(self.config.legacy_session_dir)
            if legacy_dir not in candidates:
                candidates.append(legacy_dir)
        return candidates

    def _load_from_local(self) -> GarminAuthState:
        for session_dir in self._candidate_dirs():
            oauth2_path = session_dir / "oauth2_token.json"
            if not oauth2_path.exists():
                continue

            with oauth2_path.open("r", encoding="utf-8") as file_handle:
                oauth2_token = OAuth2Token.from_dict(json.load(file_handle))

            oauth1_path = session_dir / "oauth1_token.json"
            oauth1_token = None
            if oauth1_path.exists():
                with oauth1_path.open("r", encoding="utf-8") as file_handle:
                    oauth1_raw = json.load(file_handle)
                if oauth1_raw:
                    oauth1_token = OAuth1Token.from_dict(oauth1_raw)

            return GarminAuthState(oauth2_token=oauth2_token, oauth1_token=oauth1_token)

        searched = ", ".join(str(path) for path in self._candidate_dirs())
        raise FileNotFoundError(f"No Garmin oauth2 token found in: {searched}")

    def _save_to_local(self, auth_state: GarminAuthState) -> None:
        session_dir = Path(self.config.session_dir)
        session_dir.mkdir(parents=True, exist_ok=True)

        oauth2_path = session_dir / "oauth2_token.json"
        with oauth2_path.open("w", encoding="utf-8") as file_handle:
            json.dump(auth_state.oauth2_token.to_dict(), file_handle)

        oauth1_path = session_dir / "oauth1_token.json"
        if auth_state.oauth1_token is None:
            if oauth1_path.exists():
                oauth1_path.unlink()
            return

        with oauth1_path.open("w", encoding="utf-8") as file_handle:
            json.dump(auth_state.oauth1_token.to_dict(), file_handle)

    def _load_from_aws(self) -> GarminAuthState:
        oauth2_secret_name = self.config.secret_name
        oauth1_secret_name = oauth2_secret_name.replace("oauth2_token", "oauth1_token")
        client = boto3.client("secretsmanager", region_name=self.config.region)

        try:
            oauth2_raw = client.get_secret_value(SecretId=oauth2_secret_name)["SecretString"]
        except ClientError as exc:
            raise GarminTokenStoreError(
                f"Unable to load Garmin oauth2 secret {oauth2_secret_name!r}."
            ) from exc

        oauth1_token = None
        try:
            oauth1_raw = client.get_secret_value(SecretId=oauth1_secret_name)["SecretString"]
        except ClientError:
            oauth1_raw = None

        if oauth1_raw:
            oauth1_json = json.loads(oauth1_raw)
            if oauth1_json:
                oauth1_token = OAuth1Token.from_dict(oauth1_json)

        oauth2_token = OAuth2Token.from_dict(json.loads(oauth2_raw))
        return GarminAuthState(oauth2_token=oauth2_token, oauth1_token=oauth1_token)

    def _save_to_aws(self, auth_state: GarminAuthState) -> None:
        oauth2_secret_name = self.config.secret_name
        oauth1_secret_name = oauth2_secret_name.replace("oauth2_token", "oauth1_token")
        client = boto3.client("secretsmanager", region_name=self.config.region)

        self._upsert_secret(
            client,
            oauth2_secret_name,
            json.dumps(auth_state.oauth2_token.to_dict()),
        )

        oauth1_payload = {}
        if auth_state.oauth1_token is not None:
            oauth1_payload = auth_state.oauth1_token.to_dict()
        self._upsert_secret(client, oauth1_secret_name, json.dumps(oauth1_payload))

    def _upsert_secret(self, client: Any, secret_name: str, secret_value: str) -> None:
        try:
            client.put_secret_value(SecretId=secret_name, SecretString=secret_value)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if error_code != "ResourceNotFoundException":
                raise GarminTokenStoreError(
                    f"Unable to save Garmin secret {secret_name!r}."
                ) from exc
            client.create_secret(Name=secret_name, SecretString=secret_value)