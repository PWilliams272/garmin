"""Repo-owned Garmin Connect authentication and token refresh logic."""

from __future__ import annotations

import base64
import json
import os
import random
import time
from typing import Any

import requests

from .exceptions import (
    GarminAuthenticationError,
    GarminConnectionError,
    GarminMFARequiredError,
    GarminRateLimitError,
)
from .tokens import GarminAuthState, OAuth2Token

try:
    from curl_cffi import requests as curl_requests

    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False


IOS_SSO_CLIENT_ID = "GCM_IOS_DARK"
IOS_SERVICE_PATH = "/gcm/ios"
IOS_LOGIN_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)

PORTAL_SSO_CLIENT_ID = "GarminConnect"
PORTAL_SERVICE_PATH = "/app"
PORTAL_LOGIN_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

NATIVE_API_USER_AGENT = "GCM-Android-5.23"
NATIVE_X_GARMIN_USER_AGENT = (
    "com.garmin.android.apps.connectmobile/5.23; ; Google/sdk_gphone64_arm64/google; "
    "Android/33; Dalvik/2.1.0"
)

DI_GRANT_TYPE = "https://connectapi.garmin.com/di-oauth2-service/oauth/grant/service_ticket"
DI_CLIENT_IDS = (
    "GARMIN_CONNECT_MOBILE_ANDROID_DI_2025Q2",
    "GARMIN_CONNECT_MOBILE_ANDROID_DI_2024Q4",
    "GARMIN_CONNECT_MOBILE_ANDROID_DI",
    "GARMIN_CONNECT_MOBILE_IOS_DI",
)

PORTAL_DELAY_MIN_SECONDS = 10.0
PORTAL_DELAY_MAX_SECONDS = 20.0


def _native_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": NATIVE_API_USER_AGENT,
        "X-Garmin-User-Agent": NATIVE_X_GARMIN_USER_AGENT,
        "X-Garmin-Paired-App-Version": "10861",
        "X-Garmin-Client-Platform": "Android",
        "X-App-Ver": "10861",
        "X-Lang": "en",
        "X-GCExperience": "GC5",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if extra:
        headers.update(extra)
    return headers


def _build_basic_auth(client_id: str) -> str:
    raw = f"{client_id}:".encode("utf-8")
    return f"Basic {base64.b64encode(raw).decode('ascii')}"


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _token_from_di_payload(payload: dict[str, Any], fallback_client_id: str) -> OAuth2Token:
    access_token = payload["access_token"]
    jwt_payload = _decode_jwt_payload(access_token)
    expires_at = jwt_payload.get("exp")
    client_id = jwt_payload.get("client_id") or fallback_client_id

    return OAuth2Token(
        access_token=access_token,
        refresh_token=payload.get("refresh_token"),
        token_type=payload.get("token_type", "Bearer"),
        expires_in=payload.get("expires_in"),
        expires_at=int(expires_at) if expires_at is not None else None,
        refresh_token_expires_in=payload.get("refresh_token_expires_in"),
        refresh_token_expires_at=payload.get("refresh_token_expires_at"),
        scope=payload.get("scope"),
        jti=payload.get("jti"),
        extra={
            "client_id": client_id,
            "auth_scheme": "di",
        },
    )


class GarminAuthenticator:
    """Performs Garmin credential login and DI token refresh."""

    def __init__(self, *, domain: str = "garmin.com", timeout: int = 30) -> None:
        self.domain = domain
        self.timeout = timeout
        self._sso_base = f"https://sso.{domain}"
        self._connect_base = f"https://connect.{domain}"
        self._ios_service_url = f"https://mobile.integration.{domain}{IOS_SERVICE_PATH}"
        self._portal_service_url = f"https://connect.{domain}{PORTAL_SERVICE_PATH}"
        self._di_token_url = f"https://diauth.{domain}/di-oauth2-service/oauth/token"

    def login(self, username: str, password: str) -> GarminAuthState:
        strategies = [
            ("mobile+cffi", lambda: self._mobile_login(username, password, use_cffi=True)),
            ("mobile+requests", lambda: self._mobile_login(username, password, use_cffi=False)),
            ("portal+cffi", lambda: self._portal_login(username, password, use_cffi=True)),
            ("portal+requests", lambda: self._portal_login(username, password, use_cffi=False)),
        ]

        last_error: Exception | None = None
        for _, strategy in strategies:
            try:
                return strategy()
            except GarminAuthenticationError:
                raise
            except GarminMFARequiredError:
                raise
            except GarminRateLimitError as exc:
                last_error = exc
                continue
            except GarminConnectionError as exc:
                last_error = exc
                continue

        if last_error is None:
            raise GarminConnectionError("No Garmin login strategy was available.")
        raise GarminConnectionError(f"All Garmin login strategies failed: {last_error}")

    def refresh(self, oauth2_token: OAuth2Token) -> OAuth2Token:
        refresh_token = oauth2_token.refresh_token
        client_id = oauth2_token.extra.get("client_id")
        if not refresh_token or not client_id:
            raise GarminAuthenticationError(
                "Stored Garmin token is missing a refresh token or client_id."
            )

        response = requests.post(
            self._di_token_url,
            headers=_native_headers(
                {
                    "Authorization": _build_basic_auth(str(client_id)),
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cache-Control": "no-cache",
                }
            ),
            data={
                "grant_type": "refresh_token",
                "client_id": str(client_id),
                "refresh_token": refresh_token,
            },
            timeout=self.timeout,
        )
        if response.status_code == 429:
            raise GarminRateLimitError("Garmin DI token refresh returned HTTP 429.")
        if not response.ok:
            raise GarminAuthenticationError(
                f"Garmin DI token refresh failed: HTTP {response.status_code} {response.text[:200]}"
            )

        return _token_from_di_payload(response.json(), str(client_id))

    def _mobile_login(self, username: str, password: str, *, use_cffi: bool) -> GarminAuthState:
        session = self._make_session(use_cffi=use_cffi, impersonate="safari_ios")
        response = session.post(
            f"{self._sso_base}/mobile/api/login",
            params={
                "clientId": IOS_SSO_CLIENT_ID,
                "locale": "en-US",
                "service": self._ios_service_url,
            },
            headers={
                "User-Agent": IOS_LOGIN_USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": self._sso_base,
            },
            json={
                "username": username,
                "password": password,
                "rememberMe": True,
                "captchaToken": "",
            },
            timeout=self.timeout,
        )
        return self._handle_login_response(response, self._ios_service_url)

    def _portal_login(self, username: str, password: str, *, use_cffi: bool) -> GarminAuthState:
        session = self._make_session(use_cffi=use_cffi, impersonate="chrome")
        signin_url = f"{self._sso_base}/portal/sso/en-US/sign-in"
        query = {
            "clientId": PORTAL_SSO_CLIENT_ID,
            "service": self._portal_service_url,
        }
        session.get(
            signin_url,
            params=query,
            headers={
                "User-Agent": PORTAL_LOGIN_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=self.timeout,
        )

        min_delay = float(os.environ.get("GARMIN_PORTAL_DELAY_MIN_SECONDS", PORTAL_DELAY_MIN_SECONDS))
        max_delay = float(os.environ.get("GARMIN_PORTAL_DELAY_MAX_SECONDS", PORTAL_DELAY_MAX_SECONDS))
        if min_delay > 0 and max_delay >= min_delay:
            time.sleep(random.uniform(min_delay, max_delay))

        response = session.post(
            f"{self._sso_base}/portal/api/login",
            params={
                "clientId": PORTAL_SSO_CLIENT_ID,
                "locale": "en-US",
                "service": self._portal_service_url,
            },
            headers={
                "User-Agent": PORTAL_LOGIN_USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Content-Type": "application/json",
                "Origin": self._sso_base,
                "Referer": f"{signin_url}?clientId={PORTAL_SSO_CLIENT_ID}&service={self._portal_service_url}",
            },
            json={
                "username": username,
                "password": password,
                "rememberMe": True,
                "captchaToken": "",
            },
            timeout=self.timeout,
        )
        return self._handle_login_response(response, self._portal_service_url)

    def _handle_login_response(self, response: Any, service_url: str) -> GarminAuthState:
        if response.status_code == 429:
            raise GarminRateLimitError("Garmin login returned HTTP 429.")
        if response.status_code == 403:
            raise GarminConnectionError("Garmin login was blocked by Cloudflare (HTTP 403).")
        if response.status_code == 401:
            raise GarminAuthenticationError("Garmin rejected the supplied credentials (HTTP 401).")

        try:
            payload = response.json()
        except ValueError as exc:
            raise GarminConnectionError(
                f"Garmin login returned a non-JSON response (HTTP {response.status_code})."
            ) from exc

        response_type = payload.get("responseStatus", {}).get("type")
        if response_type == "SUCCESSFUL":
            ticket = payload.get("serviceTicketId")
            if not ticket:
                raise GarminConnectionError("Garmin login succeeded without a service ticket.")
            oauth2_token = self._exchange_service_ticket(ticket, service_url)
            return GarminAuthState(oauth2_token=oauth2_token)

        if response_type == "INVALID_USERNAME_PASSWORD":
            raise GarminAuthenticationError("Garmin reported invalid username or password.")

        if response_type == "MFA_REQUIRED":
            raise GarminMFARequiredError(
                "Garmin now requires MFA for this account or session. "
                "The current repo-owned login flow only supports password-only accounts."
            )

        if response_type == "CAPTCHA_REQUIRED":
            raise GarminConnectionError("Garmin requested CAPTCHA verification for this login attempt.")

        if payload.get("error", {}).get("status-code") == "429":
            raise GarminRateLimitError("Garmin login was rate-limited by the backend.")

        raise GarminConnectionError(f"Unexpected Garmin login response: {payload}")

    def _exchange_service_ticket(self, ticket: str, service_url: str) -> OAuth2Token:
        last_error: str | None = None
        for client_id in DI_CLIENT_IDS:
            response = requests.post(
                self._di_token_url,
                headers=_native_headers(
                    {
                        "Authorization": _build_basic_auth(client_id),
                        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Cache-Control": "no-cache",
                    }
                ),
                data={
                    "client_id": client_id,
                    "service_ticket": ticket,
                    "grant_type": DI_GRANT_TYPE,
                    "service_url": service_url,
                },
                timeout=self.timeout,
            )

            if response.status_code == 429:
                raise GarminRateLimitError("Garmin DI token exchange returned HTTP 429.")
            if not response.ok:
                last_error = f"{client_id}: HTTP {response.status_code}"
                continue
            try:
                return _token_from_di_payload(response.json(), client_id)
            except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
                last_error = f"{client_id}: invalid token payload ({exc})"

        raise GarminAuthenticationError(
            f"Garmin service ticket exchange failed for all known DI client IDs. Last error: {last_error}"
        )

    def _make_session(self, *, use_cffi: bool, impersonate: str) -> Any:
        if use_cffi and HAS_CURL_CFFI:
            return curl_requests.Session(impersonate=impersonate)
        return requests.Session()