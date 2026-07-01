"""Bootstrap repo-owned Garmin tokens into AWS Secrets Manager."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from garmin._paths import get_data_dir
from garmin.api.storage import TokenStore, TokenStoreConfig


def publish_local_tokens_to_aws(
    *,
    session_dir: str | Path,
    secret_name: str,
    region: str,
    legacy_session_dir: str | Path | None = None,
) -> dict[str, str]:
    """Copy repo-owned local Garmin tokens into AWS Secrets Manager."""

    local_store = TokenStore(
        TokenStoreConfig(
            session_dir=str(session_dir),
            legacy_session_dir=str(legacy_session_dir) if legacy_session_dir else None,
            use_aws_secrets=False,
        )
    )
    auth_state = local_store.load()

    aws_store = TokenStore(
        TokenStoreConfig(
            session_dir=str(session_dir),
            use_aws_secrets=True,
            secret_name=secret_name,
            region=region,
        )
    )
    aws_store.save(auth_state)

    return {
        "oauth2_secret": secret_name,
        "oauth1_secret": secret_name.replace("oauth2_token", "oauth1_token"),
        "region": region,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish repo-owned local Garmin tokens to AWS Secrets Manager.",
    )
    default_session_dir = get_data_dir() / "sessions" / "garmin_connect"
    default_legacy_dir = get_data_dir() / "sessions" / "garth"
    parser.add_argument(
        "--session-dir",
        default=str(default_session_dir),
        help="Directory containing repo-owned local oauth token files.",
    )
    parser.add_argument(
        "--legacy-session-dir",
        default=str(default_legacy_dir),
        help="Optional legacy fallback directory if the repo-owned token file is missing.",
    )
    parser.add_argument(
        "--secret-name",
        default=os.environ.get("GARMIN_AWS_SECRET_NAME", "garmin/oauth2_token"),
        help="Secrets Manager name for the OAuth2 token JSON.",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION", "us-east-2"),
        help="AWS region for Secrets Manager.",
    )
    return parser


def main() -> None:
    load_dotenv()
    args = build_parser().parse_args()
    result = publish_local_tokens_to_aws(
        session_dir=args.session_dir,
        legacy_session_dir=args.legacy_session_dir,
        secret_name=args.secret_name,
        region=args.region,
    )
    print(
        "Published Garmin token secrets:",
        f"oauth2={result['oauth2_secret']}",
        f"oauth1={result['oauth1_secret']}",
        f"region={result['region']}",
    )


if __name__ == "__main__":
    main()