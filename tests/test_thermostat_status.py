"""Synthetic regression cases for inferred thermostat status compatibility."""

from __future__ import annotations

import pytest

from custom_components.aprilaire_cloud.models import DeviceRecord, HierarchyDevice
from custom_components.aprilaire_cloud.profiles.thermostat import ThermostatProfile

from .common import LOCATION_ID, THERMOSTAT_DEVICE_ID


def _operating_state(status: dict, mode: str = "auto") -> str | None:
    """Normalize a synthetic zone through the public profile boundary."""
    record = DeviceRecord(
        hierarchy=HierarchyDevice(
            device_id=THERMOSTAT_DEVICE_ID,
            location_id=LOCATION_ID,
            location_name="Test location",
        ),
        device_status={"model": "8920W_GS"},
        device_settings={"thermostatPZ1": {"mode": mode}},
        status_payloads={"thermostatPZ1": status},
    )
    return ThermostatProfile().normalize(record).zones["PZ1"].operating_state


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("coolingStatus", "stage1", "cooling"),
        ("coolingStatus", "stage2", "cooling"),
        ("coolingStatus", "stage3", "cooling"),
        ("coolingStatus", "stage4", "cooling"),
        ("coolingStatus", "cool", "cooling"),
        ("heatingStatus", "stage1", "heating"),
        ("heatingStatus", "stage2", "heating"),
        ("heatingStatus", "stage3", "heating"),
        ("heatingStatus", "stage4", "heating"),
        ("heatingStatus", "heat", "heating"),
        ("heatingStatus", "aux-heat", "heating"),
        ("heatingStatus", "emergency-heat", "heating"),
        ("coolingStatus", " Stage2 ", "cooling"),
        ("heatingStatus", "AUX_HEAT", "heating"),
        ("heatingStatus", "Emergency Heat", "heating"),
    ],
)
@pytest.mark.parametrize("fan_on", [True, False, None])
def test_active_split_statuses(field, value, expected, fan_on) -> None:
    """Active stages and aliases take precedence over fan-only activity."""
    status = {"heatingStatus": "inactive", "coolingStatus": "inactive", field: value}
    if fan_on is not None:
        status["isFanOn"] = fan_on
    assert _operating_state(status) == expected


@pytest.mark.parametrize(
    ("status", "mode", "expected"),
    [
        ({"coolingStatus": "stage2", "isFanOn": True}, "off", "off"),
        ({"heatingStatus": "stage3", "coolingStatus": "stage2"}, "auto", "heating"),
        ({"coolingStatus": "stage2", "equipmentStatus": "heating"}, "auto", "cooling"),
        ({"coolingStatus": "inactive", "isFanOn": False}, "cool", "idle"),
        ({"heatingStatus": "off", "coolingStatus": "idle", "isFanOn": True}, "auto", "fan"),
        ({"heatingStatus": "off", "coolingStatus": "idle", "isFanOn": False}, "auto", "idle"),
        ({"coolingStatus": "vendor-unknown", "isFanOn": True}, "cool", "fan"),
        ({"coolingStatus": "vendor-unknown", "equipmentStatus": "cooling"}, "auto", "cooling"),
        ({"equipmentStatus": "cool"}, "auto", "cooling"),
        ({"equipmentStatus": "vendor-unknown"}, "auto", None),
        ({"coolingStatus": "vendor-unknown", "isFanOn": False}, "cool", None),
        ({"coolingStatus": "stage-unknown", "isFanOn": False}, "auto", None),
        ({"coolingStatus": "", "isFanOn": False}, "auto", None),
        ({"coolingStatus": 2, "isFanOn": False}, "auto", None),
        ({"coolingStatus": None, "isFanOn": False}, "auto", None),
        ({"coolingStatus": True, "isFanOn": False}, "auto", None),
        ({}, "cool", None),
    ],
)
def test_status_priority_and_conservative_fallbacks(status, mode, expected) -> None:
    """Mode alone cannot establish activity; existing precedence is preserved."""
    assert _operating_state(status, mode) == expected
