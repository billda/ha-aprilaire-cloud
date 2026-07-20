"""AprilAire vendor transport boundary."""

from .auth import (
    AprilaireCloudAuthenticationError,
    AprilaireCloudAuthenticationProtocolError,
    AprilaireCloudAuthenticationTransientError,
    AprilaireCloudInvalidCredentialsError,
    AuthOperation,
    CognitoAuthProvider,
)
from .client import (
    ApiErrorContext,
    AprilaireCloudApiClient,
    AprilaireCloudApiError,
    AprilaireCloudCommunicationError,
    AprilaireCloudRateLimitError,
    AprilaireCloudWriteError,
)

__all__ = [
    "ApiErrorContext",
    "AprilaireCloudApiClient",
    "AprilaireCloudApiError",
    "AprilaireCloudAuthenticationError",
    "AprilaireCloudAuthenticationProtocolError",
    "AprilaireCloudAuthenticationTransientError",
    "AprilaireCloudCommunicationError",
    "AprilaireCloudInvalidCredentialsError",
    "AprilaireCloudRateLimitError",
    "AprilaireCloudWriteError",
    "AuthOperation",
    "CognitoAuthProvider",
]
