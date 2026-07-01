"""Minimal Garmin Connect API client using repo-owned request logic."""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urljoin

from requests import HTTPError, Response, Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .exceptions import GarminAuthenticationError, GarminRequestError
from .tokens import OAuth2Token


DEFAULT_USER_AGENT = "garmin-repo/0.1"


class GarminConnectClient:
    """Authenticated client for Garmin Connect API requests."""

    def __init__(
        self,
        *,
        domain: str = "garmin.com",
        session: Session | None = None,
        timeout: int = 10,
        retries: int = 3,
        status_forcelist: tuple[int, ...] = (408, 429, 500, 502, 503, 504),
        backoff_factor: float = 0.5,
        refresh_callback: Callable[[], OAuth2Token] | None = None,
    ) -> None:
        self.domain = domain
        self.session = session or Session()
        self.timeout = timeout
        self.refresh_callback = refresh_callback
        self.oauth2_token: OAuth2Token | None = None

        retry = Retry(
            total=retries,
            status_forcelist=status_forcelist,
            backoff_factor=backoff_factor,
            allowed_methods=None,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.headers.setdefault("User-Agent", DEFAULT_USER_AGENT)

    def set_oauth2_token(self, token: OAuth2Token) -> None:
        self.oauth2_token = token

    def request(self, method: str, path: str, **kwargs: Any) -> Response:
        if self.oauth2_token is None:
            raise GarminAuthenticationError("No Garmin oauth2 token is loaded.")

        if self.oauth2_token.expired:
            if self.refresh_callback is None:
                raise GarminAuthenticationError(
                    "Stored Garmin oauth2 token is expired and no refresh callback is configured."
                )
            self.oauth2_token = self.refresh_callback()

        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = self.oauth2_token.authorization_header
        url = urljoin(f"https://connectapi.{self.domain}/", path.lstrip("/"))
        response = self.session.request(
            method,
            url,
            headers=headers,
            timeout=self.timeout,
            **kwargs,
        )
        if response.status_code == 401 and self.refresh_callback is not None:
            self.oauth2_token = self.refresh_callback()
            headers["Authorization"] = self.oauth2_token.authorization_header
            response = self.session.request(
                method,
                url,
                headers=headers,
                timeout=self.timeout,
                **kwargs,
            )
        try:
            response.raise_for_status()
        except HTTPError as exc:
            raise GarminRequestError(
                "Garmin Connect request failed.",
                status_code=response.status_code,
                method=method,
                url=url,
                response_text=response.text,
            ) from exc
        return response

    def connectapi(self, path: str, *, method: str = "GET", **kwargs: Any) -> dict[str, Any] | list[Any] | None:
        response = self.request(method, path, **kwargs)
        if response.status_code == 204:
            return None
        return response.json()

    def get(self, path: str, **kwargs: Any) -> dict[str, Any] | list[Any] | None:
        return self.connectapi(path, method="GET", **kwargs)

    def post(self, path: str, **kwargs: Any) -> dict[str, Any] | list[Any] | None:
        return self.connectapi(path, method="POST", **kwargs)