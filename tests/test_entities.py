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
from custom_components.aprilaire_cloud.const import CONF_ENABLE_EXTRA_DIAGNOSTICS, DOMAIN
from custom_components.aprilaire_cloud.coordinator import AprilaireCloudDataUpdateCoordinator
from custom_components.aprilaire_cloud.entity import raise_ha_write_error
from custom_components.aprilaire_cloud.humidifier import AprilaireCloudHumidifierEntity
from custom_components.aprilaire_cloud.number import AprilaireAlertLimitNumber
from custom_components.aprilaire_cloud.sensor import (
    DEHUMIDIFIER_SENSORS,
    AprilaireStaticSensorEntity,
)

from .common import (
    LOCATION_ID,
    PASSWORD,
    USERNAME,
    FakeClient,
    FakeWebSocket,
    bootstrap_coordinator,
    build_user,
)


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
        DEHUMIDIFIER_SENSORS["current_humidity"],
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


async def test_websocket_status_entity_reflects_connection_state(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """WebSocket status entity should reflect socket state."""
    from custom_components.aprilaire_cloud.binary_sensor import AprilaireWebSocketStatusEntity
    from custom_components.aprilaire_cloud.models import SocketState

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

    entity = AprilaireWebSocketStatusEntity(coordinator, LOCATION_ID)

    assert entity.is_on is True
    assert entity.available is True
    assert entity.extra_state_attributes["reconnect_attempt"] == 0
    assert entity.extra_state_attributes["last_error"] is None

    await coordinator.async_socket_state_changed(
        SocketState(
            location_id=LOCATION_ID,
            connected=False,
            initial_sync_complete=False,
            reconnect_attempt=3,
            last_error="Connection reset",
        )
    )

    assert entity.is_on is False
    assert entity.extra_state_attributes["reconnect_attempt"] == 3
    assert entity.extra_state_attributes["last_error"] == "Connection reset"


async def test_websocket_status_entity_honors_extra_diagnostics_default(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """WebSocket status entity should follow the extra diagnostics opt-in."""
    from custom_components.aprilaire_cloud.binary_sensor import AprilaireWebSocketStatusEntity

    client = FakeClient()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket
    )

    default_entry = MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=f"{build_user()['userId']}-default",
        data={"username": USERNAME, "password": PASSWORD},
    )
    default_entry.add_to_hass(hass)
    default_coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=default_entry, client=client
    )
    await bootstrap_coordinator(default_coordinator)

    default_entity = AprilaireWebSocketStatusEntity(default_coordinator, LOCATION_ID)
    assert default_entity.entity_registry_enabled_default is False

    diagnostics_entry = MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=f"{build_user()['userId']}-diagnostics",
        data={"username": USERNAME, "password": PASSWORD},
        options={CONF_ENABLE_EXTRA_DIAGNOSTICS: True},
    )
    diagnostics_entry.add_to_hass(hass)
    diagnostics_coordinator = AprilaireCloudDataUpdateCoordinator(
        hass, config_entry=diagnostics_entry, client=client
    )
    await bootstrap_coordinator(diagnostics_coordinator)

    diagnostics_entity = AprilaireWebSocketStatusEntity(diagnostics_coordinator, LOCATION_ID)
    assert diagnostics_entity.entity_registry_enabled_default is True


def test_typed_write_errors_map_to_home_assistant_errors() -> None:
    """Typed integration failures should become translated Home Assistant errors."""
    with pytest.raises(HomeAssistantError) as rate_limited:
        raise_ha_write_error(AprilaireCloudRateLimitError(12))

    assert rate_limited.value.translation_key == "rate_limited"

    with pytest.raises(HomeAssistantError) as write_failed:
        raise_ha_write_error(AprilaireCloudWriteError("write failed"))

    assert write_failed.value.translation_key == "write_failed"
