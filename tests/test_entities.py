"""Entity behavior tests for AprilAire Cloud."""

from __future__ import annotations

from dataclasses import replace

import pytest
from homeassistant.components.humidifier import HumidifierAction
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aprilaire_cloud.api import (
    AprilaireCloudRateLimitError,
    AprilaireCloudWriteError,
)
from custom_components.aprilaire_cloud.const import DOMAIN
from custom_components.aprilaire_cloud.coordinator import AprilaireCloudDataUpdateCoordinator
from custom_components.aprilaire_cloud.entity import raise_ha_write_error
from custom_components.aprilaire_cloud.humidifier import AprilaireCloudHumidifierEntity
from custom_components.aprilaire_cloud.number import AprilaireAlertLimitNumber
from custom_components.aprilaire_cloud.sensor import STATIC_SENSORS, AprilaireStaticSensorEntity

from .common import PASSWORD, USERNAME, build_user
from .test_coordinator import FakeClient, FakeWebSocket, bootstrap_coordinator


async def test_humidifier_action_and_sensor_values(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Entities should map coordinator data to HA state correctly."""
    client = FakeClient()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=build_user()["userId"],
        data={"username": USERNAME, "password": PASSWORD},
    )
    entry.add_to_hass(hass)

    coordinator = AprilaireCloudDataUpdateCoordinator(hass, config_entry=entry, client=client)
    await bootstrap_coordinator(coordinator)

    humidifier = AprilaireCloudHumidifierEntity(
        coordinator, coordinator.data.supported_device_ids[0]
    )
    current_humidity_sensor = AprilaireStaticSensorEntity(
        coordinator,
        coordinator.data.supported_device_ids[0],
        STATIC_SENSORS["current_humidity"],
    )

    assert humidifier.is_on is True
    assert humidifier.target_humidity == 52
    assert humidifier.action is HumidifierAction.IDLE
    assert current_humidity_sensor.native_value == 49
    assert "location" not in humidifier.extra_state_attributes
    assert humidifier.extra_state_attributes["equipment_status"] == "inactive"


async def test_entities_read_effective_pending_settings(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Humidifier and number entities should use optimistic pending settings."""
    client = FakeClient()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=build_user()["userId"],
        data={"username": USERNAME, "password": PASSWORD},
    )
    entry.add_to_hass(hass)

    coordinator = AprilaireCloudDataUpdateCoordinator(hass, config_entry=entry, client=client)
    await bootstrap_coordinator(coordinator)

    device_id = coordinator.data.supported_device_ids[0]
    device = coordinator.data.devices[device_id]
    coordinator._devices[device_id] = replace(
        device,
        pending_device_settings={
            "dehumidifier": {
                "humiditySetpoint": 60,
                "mode": "off",
                "alertLimits": {"highHum": 70},
            }
        },
    )
    coordinator.async_set_updated_data(coordinator._build_snapshot())

    humidifier = AprilaireCloudHumidifierEntity(coordinator, device_id)
    alert_limit = AprilaireAlertLimitNumber(coordinator, device_id, "highHum")

    assert humidifier.target_humidity == 60
    assert humidifier.is_on is False
    assert alert_limit.native_value == 70
    assert alert_limit.entity_category.value == "config"


def test_typed_write_errors_map_to_home_assistant_errors() -> None:
    """Typed integration failures should become translated Home Assistant errors."""
    with pytest.raises(HomeAssistantError) as rate_limited:
        raise_ha_write_error(AprilaireCloudRateLimitError(12))

    assert rate_limited.value.translation_key == "rate_limited"

    with pytest.raises(HomeAssistantError) as write_failed:
        raise_ha_write_error(AprilaireCloudWriteError("write failed"))

    assert write_failed.value.translation_key == "write_failed"
