import json

from garmin.api.storage import TokenStore, TokenStoreConfig
from garmin.api.tokens import GarminAuthState, OAuth2Token
from garmin.scripts.bootstrap_aws_auth import publish_local_tokens_to_aws


class FakeSecretsClient:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def put_secret_value(self, *, SecretId: str, SecretString: str) -> None:
        self.values[SecretId] = SecretString


def test_publish_local_tokens_to_aws_writes_oauth2_and_oauth1_placeholder(
    tmp_path, monkeypatch
) -> None:
    session_dir = tmp_path / "sessions" / "garmin_connect"
    local_store = TokenStore(
        TokenStoreConfig(session_dir=str(session_dir), use_aws_secrets=False)
    )
    local_store.save(
        GarminAuthState(
            oauth2_token=OAuth2Token(
                access_token="access-token",
                refresh_token="refresh-token",
                expires_at=1234567890,
                extra={"client_id": "client-123"},
            )
        )
    )

    fake_client = FakeSecretsClient()
    monkeypatch.setattr("garmin.api.storage.boto3.client", lambda *args, **kwargs: fake_client)

    result = publish_local_tokens_to_aws(
        session_dir=session_dir,
        secret_name="garmin/oauth2_token",
        region="us-east-2",
    )

    assert result == {
        "oauth2_secret": "garmin/oauth2_token",
        "oauth1_secret": "garmin/oauth1_token",
        "region": "us-east-2",
    }
    assert json.loads(fake_client.values["garmin/oauth2_token"])["access_token"] == "access-token"
    assert json.loads(fake_client.values["garmin/oauth1_token"]) == {}