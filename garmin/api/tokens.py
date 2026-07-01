"""Token models used by the Garmin Connect client boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any


@dataclass(slots=True)
class OAuth1Token:
    """Stored OAuth1 token fields used by older Garmin Connect flows."""

    oauth_token: str
    oauth_token_secret: str
    mfa_token: str | None = None
    mfa_expiration_timestamp: str | None = None
    domain: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OAuth1Token":
        return cls(
            oauth_token=data["oauth_token"],
            oauth_token_secret=data["oauth_token_secret"],
            mfa_token=data.get("mfa_token"),
            mfa_expiration_timestamp=data.get("mfa_expiration_timestamp"),
            domain=data.get("domain"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "oauth_token": self.oauth_token,
            "oauth_token_secret": self.oauth_token_secret,
            "mfa_token": self.mfa_token,
            "mfa_expiration_timestamp": self.mfa_expiration_timestamp,
            "domain": self.domain,
        }


@dataclass(slots=True)
class OAuth2Token:
    """Stored OAuth2 token fields used for authenticated Connect API access."""

    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_in: int | None = None
    expires_at: int | None = None
    refresh_token_expires_in: int | None = None
    refresh_token_expires_at: int | None = None
    scope: str | None = None
    jti: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OAuth2Token":
        known_fields = {
            "access_token",
            "refresh_token",
            "token_type",
            "expires_in",
            "expires_at",
            "refresh_token_expires_in",
            "refresh_token_expires_at",
            "scope",
            "jti",
        }
        extra = {key: value for key, value in data.items() if key not in known_fields}
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            token_type=data.get("token_type", "Bearer"),
            expires_in=data.get("expires_in"),
            expires_at=data.get("expires_at"),
            refresh_token_expires_in=data.get("refresh_token_expires_in"),
            refresh_token_expires_at=data.get("refresh_token_expires_at"),
            scope=data.get("scope"),
            jti=data.get("jti"),
            extra=extra,
        )

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= int(time())

    @property
    def refresh_expired(self) -> bool:
        return (
            self.refresh_token_expires_at is not None
            and self.refresh_token_expires_at <= int(time())
        )

    @property
    def authorization_header(self) -> str:
        token_type = self.token_type or "Bearer"
        return f"{token_type.title()} {self.access_token}"

    def to_dict(self) -> dict[str, Any]:
        data = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
            "expires_at": self.expires_at,
            "refresh_token_expires_in": self.refresh_token_expires_in,
            "refresh_token_expires_at": self.refresh_token_expires_at,
            "scope": self.scope,
            "jti": self.jti,
        }
        data.update(self.extra)
        return {key: value for key, value in data.items() if value is not None}


@dataclass(slots=True)
class GarminAuthState:
    """Complete stored authentication state for Garmin Connect requests."""

    oauth2_token: OAuth2Token
    oauth1_token: OAuth1Token | None = None