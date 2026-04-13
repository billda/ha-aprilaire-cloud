"""Shared test fixtures, payload factories, and test doubles."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import ClassVar

from custom_components.aprilaire_cloud.api import AprilaireCloudRateLimitError
from custom_components.aprilaire_cloud.coordinator import AprilaireCloudDataUpdateCoordinator
from custom_components.aprilaire_cloud.models import SocketState

USERNAME = "billda@gmail.com"
PASSWORD = "Trueblue1!"
USER_ID = "user-123"
DEVICE_ID = "BC8D7EEC97E2"
SECOND_DEVICE_ID = "DEADBEEF0001"
LOCATION_ID = "bcf1939c-1111-2222-3333-a80ac86d"
SECOND_LOCATION_ID = "bcf1939c-4444-5555-6666-a80ac86d"


def build_user() -> dict:
    """Return a sample user payload."""
    return {"userId": USER_ID, "email": USERNAME}


def build_hierarchy(include_second_device: bool = False) -> dict:
    """Return a sample hierarchy payload."""
    devices = [{"deviceId": DEVICE_ID, "access": "manage", "zone": 1}]
    if include_second_device:
        devices.append({"deviceId": SECOND_DEVICE_ID, "access": "manage", "zone": 1})
    return {
        "locations": [
            {
                "locationId": LOCATION_ID,
                "name": "Home",
                "timeZone": "America/New_York",
                "rooms": [{"name": "Crawl Space", "devices": devices}],
            }
        ]
    }


def build_two_location_hierarchy() -> dict:
    """Return a sample hierarchy with one device in each location."""
    return {
        "locations": [
            {
                "locationId": LOCATION_ID,
                "name": "Home",
                "timeZone": "America/New_York",
                "rooms": [{"name": "Crawl Space", "devices": [{"deviceId": DEVICE_ID}]}],
            },
            {
                "locationId": SECOND_LOCATION_ID,
                "name": "Cabin",
                "timeZone": "America/New_York",
                "rooms": [{"name": "Basement", "devices": [{"deviceId": SECOND_DEVICE_ID}]}],
            },
        ]
    }


def build_device_status(device_id: str = DEVICE_ID, model: str = "E100W") -> dict:
    """Return a sample DeviceStatus payload."""
    return {
        "_type": "DeviceStatus",
        "deviceId": device_id,
        "asOf": "2026-03-24T00:00:00.000Z",
        "model": model,
        "hardwareRev": "D",
        "firmwareRev": "1.1.3",
        "altFirmwareRev": "1.9.0",
    }


def build_device_settings(device_id: str = DEVICE_ID, humidity: int = 52) -> dict:
    """Return a sample DeviceSettings payload."""
    return {
        "_type": "DeviceSettings",
        "deviceId": device_id,
        "asOf": "2026-03-24T00:00:01.000Z",
        "dehumidifier": {
            "mode": "on",
            "humiditySetpoint": humidity,
            "alertLimits": {"highHum": 65},
            "sensors": [
                {"uid": 1, "dispName": "Inlet Air"},
                {"uid": 4, "dispName": "Suction Line"},
                {"uid": 5, "dispName": "Discharge Line"},
            ],
        },
    }


def build_device_setup(
    device_id: str = DEVICE_ID,
    *,
    control_type: str = "internal",
    scale: str = "%RH",
) -> dict:
    """Return a sample DeviceSetup payload."""
    return {
        "_type": "DeviceSetup",
        "deviceId": device_id,
        "asOf": "2026-03-24T00:00:02.000Z",
        "dehumidifier": {
            "controlType": control_type,
            "installation": "standalone",
            "hvacType": "none",
            "scale": scale,
            "allowWithAC": False,
            "forceHvacFan": False,
            "sampleRate": 60,
            "servicePeriod": 720,
        },
        "type": "dehumidifier",
    }


def build_dehumidifier_status(device_id: str = DEVICE_ID, humidity: int = 49) -> dict:
    """Return a sample DehumidifierStatus payload."""
    return {
        "_type": "DehumidifierStatus",
        "deviceId": device_id,
        "asOf": "2026-03-24T00:00:03.000Z",
        "equipmentStatus": "inactive",
        "alerts": {
            "highTemp": False,
            "lowHum": False,
            "highHum": False,
            "lowTemp": False,
        },
        "fanTimeHours": 44,
        "filterService": {"needsService": False, "remaining": 100},
        "humSensors": [
            {
                "reading": humidity,
                "uid": 1,
                "isControlling": True,
                "type": "inlet-air",
                "isWireless": False,
                "status": "reporting",
            }
        ],
        "isCompOn": False,
        "isDehumFanOn": False,
        "isHvacFanOn": False,
        "tempSensors": [
            {
                "reading": 21.46,
                "uid": 1,
                "isControlling": True,
                "type": "inlet-air",
                "isWireless": False,
                "status": "reporting",
            },
            {
                "reading": 16.21,
                "uid": 4,
                "isControlling": False,
                "type": "suction",
                "isWireless": False,
                "status": "reporting",
            },
        ],
        "wifiRSSI": -45,
    }


def build_sensor_hub_status(device_id: str = DEVICE_ID) -> dict:
    """Return a sample SensorHubStatus payload."""
    return {
        "_type": "SensorHubStatus",
        "deviceId": device_id,
        "asOf": "2026-03-24T00:00:04.000Z",
    }


def build_initial_messages(
    device_id: str = DEVICE_ID,
    *,
    control_type: str = "internal",
    scale: str = "%RH",
) -> list[dict]:
    """Return a bootstrap websocket message batch."""
    return [
        build_dehumidifier_status(device_id),
        build_device_settings(device_id),
        build_device_setup(device_id, control_type=control_type, scale=scale),
        build_device_status(device_id),
        build_sensor_hub_status(device_id),
    ]


def deep_copy(data):
    """Return a deep copy helper."""
    return deepcopy(data)


# ---------------------------------------------------------------------------
# Fake API client (superset used by coordinator, entity, diagnostics tests)
# ---------------------------------------------------------------------------


class FakeClient:
    """Fake API client for tests that need write tracking and failure injection."""

    def __init__(self) -> None:
        """Initialize the fake client."""
        self.username = USERNAME
        self.session = object()
        self._hierarchy = build_hierarchy()
        self._rate_limit = False
        self.device_settings = build_device_settings()
        self.patched_payloads: list[dict] = []
        self.patch_started = asyncio.Event()
        self.patch_release: asyncio.Event | None = None
        self.patch_side_effect: Exception | None = None
        self.patch_side_effects: list[Exception | None] = []
        self.rest_failures: dict[tuple[str, str], Exception] = {}
        self.requested_status_ids: list[str] = []
        self.requested_status_endpoints: list[tuple[str, str]] = []
        self.requested_settings_ids: list[str] = []
        self.rate_limited_until = None

    async def async_authenticate(self) -> None:
        """No-op auth."""
        return None

    async def async_get_user(self) -> dict:
        """Return a fake account."""
        return build_user()

    async def async_get_hierarchy(self) -> dict:
        """Return the current fake hierarchy."""
        if self._rate_limit:
            raise AprilaireCloudRateLimitError(120)
        return self._hierarchy

    async def async_get_device_status(self, device_id: str) -> dict:
        """Return status."""
        self.requested_status_ids.append(device_id)
        if ("device_status", device_id) in self.rest_failures:
            raise self.rest_failures[("device_status", device_id)]
        return build_initial_messages(device_id)[3]

    async def async_get_status(self, device_id: str, endpoint: str) -> dict:
        """Return a profile-specific status payload."""
        self.requested_status_endpoints.append((device_id, endpoint))
        if ("status", f"{device_id}:{endpoint}") in self.rest_failures:
            raise self.rest_failures[("status", f"{device_id}:{endpoint}")]
        if endpoint == "dehumidifier":
            return build_dehumidifier_status(device_id)
        return {"_type": f"{endpoint.title()}Status", "deviceId": device_id}

    async def async_get_device_settings(self, device_id: str) -> dict:
        """Return device settings."""
        self.requested_settings_ids.append(device_id)
        if ("device_settings", device_id) in self.rest_failures:
            raise self.rest_failures[("device_settings", device_id)]
        return deep_copy(self.device_settings)

    async def async_patch_device_settings(self, device_id: str, payload: dict) -> None:
        """Pretend a write succeeded."""
        self.patched_payloads.append(deep_copy(payload))
        self.patch_started.set()
        if self.patch_release is not None:
            await self.patch_release.wait()
        if self.patch_side_effects:
            side_effect = self.patch_side_effects.pop(0)
            if side_effect is not None:
                raise side_effect
        if self.patch_side_effect is not None:
            raise self.patch_side_effect
        return None

    def set_remote_settings(self, payload: dict) -> None:
        """Update the fake remote settings payload."""
        self.device_settings = deep_copy(payload)


# ---------------------------------------------------------------------------
# Fake WebSocket doubles
# ---------------------------------------------------------------------------


class FakeWebSocket:
    """Fake websocket manager that injects a bootstrap message batch."""

    instances: ClassVar[dict[str, FakeWebSocket]] = {}

    def __init__(
        self,
        *,
        client,
        session,
        location_id,
        message_callback,
        state_callback,
    ) -> None:
        """Initialize the websocket."""
        self._location_id = location_id
        self._message_callback = message_callback
        self._state_callback = state_callback
        FakeWebSocket.instances[location_id] = self

    async def async_start(self) -> None:
        """Publish the initial socket state."""
        await self._state_callback(
            SocketState(location_id=self._location_id, connected=True, initial_sync_complete=False)
        )

    async def async_wait_for_initial_sync(self, wait_timeout: float) -> bool:
        """Inject bootstrap data."""
        await self._message_callback(self._location_id, build_initial_messages())
        await self._state_callback(
            SocketState(location_id=self._location_id, connected=True, initial_sync_complete=True)
        )
        return True

    async def async_stop(self) -> None:
        """Stop the websocket."""
        return None

    async def push_messages(self, messages: list[dict]) -> None:
        """Push custom websocket messages into the coordinator."""
        await self._state_callback(
            SocketState(location_id=self._location_id, connected=True, initial_sync_complete=True)
        )
        await self._message_callback(self._location_id, messages)


class MultiLocationFakeWebSocket(FakeWebSocket):
    """Fake websocket that boots the matching device for each location."""

    async def async_wait_for_initial_sync(self, wait_timeout: float) -> bool:
        """Inject bootstrap data for the matching location."""
        device_id = DEVICE_ID if self._location_id == LOCATION_ID else SECOND_DEVICE_ID
        await self._message_callback(self._location_id, build_initial_messages(device_id))
        await self._state_callback(
            SocketState(location_id=self._location_id, connected=True, initial_sync_complete=True)
        )
        return True


# ---------------------------------------------------------------------------
# Coordinator helpers
# ---------------------------------------------------------------------------


async def bootstrap_coordinator(coordinator: AprilaireCloudDataUpdateCoordinator) -> None:
    """Run the coordinator's startup path without config-entry state checks."""
    await coordinator._async_setup()
    coordinator.async_set_updated_data(coordinator._build_snapshot())


async def wait_until(predicate, *, wait_timeout: float = 1.0) -> None:
    """Wait until a predicate becomes true."""
    end = asyncio.get_running_loop().time() + wait_timeout
    while asyncio.get_running_loop().time() < end:
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("Timed out waiting for predicate")
