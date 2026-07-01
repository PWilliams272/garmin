"""Exceptions raised by the Garmin Connect client boundary."""


class GarminAPIError(Exception):
    """Base exception for Garmin client errors."""


class GarminAuthenticationError(GarminAPIError):
    """Raised when authentication state is missing, invalid, or expired."""


class GarminLoginFlowNotImplementedError(GarminAuthenticationError):
    """Raised when the repo has no implementation for the needed login step."""


class GarminTokenStoreError(GarminAuthenticationError):
    """Raised when token persistence fails."""


class GarminConnectionError(GarminAPIError):
    """Raised when Garmin authentication or transport plumbing fails."""


class GarminRateLimitError(GarminConnectionError):
    """Raised when Garmin blocks or rate-limits authentication requests."""


class GarminMFARequiredError(GarminAuthenticationError):
    """Raised when Garmin requires MFA and the current flow cannot complete it."""


class GarminRequestError(GarminAPIError):
    """Raised when an authenticated Garmin Connect request fails."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        method: str | None = None,
        url: str | None = None,
        response_text: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.method = method
        self.url = url
        self.response_text = response_text