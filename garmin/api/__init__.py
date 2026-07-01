"""Garmin Connect client boundary for the garmin package.

This package owns Garmin-specific authentication state, token persistence,
and authenticated Connect API requests. Downstream pullers should continue to
depend only on ``GarminSession``.
"""

from .exceptions import (
    GarminAPIError,
    GarminAuthenticationError,
    GarminLoginFlowNotImplementedError,
    GarminRequestError,
    GarminTokenStoreError,
)
from .session import GarminSession

__all__ = [
    "GarminAPIError",
    "GarminAuthenticationError",
    "GarminLoginFlowNotImplementedError",
    "GarminRequestError",
    "GarminSession",
    "GarminTokenStoreError",
]