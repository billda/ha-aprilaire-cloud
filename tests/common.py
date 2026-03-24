"""Shared test fixtures and payload factories."""

from __future__ import annotations

from copy import deepcopy

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
