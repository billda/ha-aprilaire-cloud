"""Constants for the AprilAire Cloud integration."""

from __future__ import annotations

from datetime import timedelta
from logging import Logger, getLogger

from homeassistant.const import Platform

LOGGER: Logger = getLogger(__package__)

DOMAIN = "aprilaire_cloud"
MANUFACTURER = "AprilAire"

PLATFORMS: list[Platform] = [
    Platform.HUMIDIFIER,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
]

ACCOUNT_API = "https://account.aprilaire.io"
DEVICE_API = "https://device.aprilaire.io"
WEBSOCKET_URL = "wss://socket.aprilaire.io/"

COGNITO_REGION = "us-west-2"
COGNITO_USER_POOL_ID = "us-west-2_skfkpmVv6"
COGNITO_CLIENT_ID = "3aiakr6qdoqtajv7qgtapecerg"

SUPPORTED_REPORTING_TYPE = "dehumidifier"
SUPPORTED_CONTROL_TYPE = "internal"
SUPPORTED_SCALE = "%RH"

AUTH_REFRESH_MARGIN_SECONDS = 5 * 60
WEBSOCKET_INITIAL_SYNC_TIMEOUT = 10
WEBSOCKET_PING_INITIAL_DELAY_SECONDS = 1
WEBSOCKET_PING_INTERVAL_SECONDS = 290
WEBSOCKET_PONG_TIMEOUT_SECONDS = 3
WEBSOCKET_RECONNECT_MIN_SECONDS = 1
WEBSOCKET_RECONNECT_MAX_SECONDS = 30

DEFAULT_SAFETY_REFRESH_INTERVAL = timedelta(minutes=15)
DEFAULT_FALLBACK_REFRESH_INTERVAL = timedelta(minutes=2)
DEFAULT_REQUEST_TIMEOUT = 20
MAX_RATE_LIMIT_RETRY_SECONDS = 300
DEFAULT_RATE_LIMIT_RETRY_SECONDS = 60
MAX_PARALLEL_REST_REQUESTS = 4
POST_WRITE_CONFIRM_TIMEOUT = 5
SHORT_WRITE_RETRY_THRESHOLD_SECONDS = 2

CONF_ACCOUNT_USER_ID = "account_user_id"
CONF_ACCOUNT_EMAIL = "account_email"

ATTRIBUTION = "Data provided by AprilAire Healthy Air"

