"""Entity behavior tests for AprilAire Cloud."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, call

import pytest
from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
)
from homeassistant.components.humidifier import HumidifierAction
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aprilaire_cloud.api import (
    AprilaireCloudRateLimitError,
    AprilaireCloudWriteError,
)
from custom_components.aprilaire_cloud.climate import AprilaireThermostatClimateEntity
from custom_components.aprilaire_cloud.const import CONF_ENABLE_EXTRA_DIAGNOSTICS, DOMAIN
from custom_components.aprilaire_cloud.coordinator import AprilaireCloudDataUpdateCoordinator
from custom_components.aprilaire_cloud.entity import raise_ha_write_error
from custom_components.aprilaire_cloud.humidifier import AprilaireCloudHumidifierEntity
from custom_components.aprilaire_cloud.number import AprilaireAlertLimitNumber
from custom_components.aprilaire_cloud.sensor import (
    DEHUMIDIFIER_SENSORS,
    AprilaireStaticSensorEntity,
    AprilaireThermostatIAQSensor,
    AprilaireThermostatZoneSensor,
)

from .common import (
    LOCATION_ID,
    PASSWORD,
    USERNAME,
    FakeClient,
    FakeWebSocket,
    ThermostatFakeWebSocket,
    bootstrap_coordinator,
    build_thermostat_hierarchy,
    build_thermostat_settings,
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


async def _bootstrap_thermostat_coordinator(hass, monkeypatch):
    """Return a coordinator bootstrapped with thermostat data."""
    client = FakeClient()
    client._hierarchy = build_thermostat_hierarchy()
    client.device_settings = build_thermostat_settings()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket",
        ThermostatFakeWebSocket,
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
    return coordinator


async def test_thermostat_climate_properties_map_to_home_assistant_values(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Thermostat climate entities should expose normalized HA properties."""
    coordinator = await _bootstrap_thermostat_coordinator(hass, monkeypatch)
    device_id = coordinator.data.supported_device_ids[0]

    pz1 = AprilaireThermostatClimateEntity(coordinator, device_id, "thermostat_pz1")
    sz2 = AprilaireThermostatClimateEntity(coordinator, device_id, "thermostat_sz2")

    assert pz1.unique_id == f"{device_id}_thermostat_pz1"
    assert pz1.current_temperature == 70
    assert pz1.current_humidity == 45
    assert pz1.hvac_mode is HVACMode.HEAT
    assert pz1.hvac_action is HVACAction.HEATING
    assert pz1.target_temperature == 68
    assert pz1.target_temperature_low == 68
    assert pz1.target_temperature_high == 75
    assert pz1.fan_mode == "auto"
    assert pz1.preset_mode == "permanent"
    assert pz1.min_temp == 45
    assert pz1.max_temp == 95

    assert sz2.unique_id == f"{device_id}_thermostat_sz2"
    assert sz2.hvac_mode is HVACMode.HEAT_COOL
    assert sz2.fan_mode == "circulate"
    assert sz2.preset_mode == "temporary"


async def test_thermostat_climate_writes_exact_patch_payloads(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Thermostat climate writes should patch only the owning zone settings key."""
    coordinator = await _bootstrap_thermostat_coordinator(hass, monkeypatch)
    device_id = coordinator.data.supported_device_ids[0]
    entity = AprilaireThermostatClimateEntity(coordinator, device_id, "thermostat_pz1")
    coordinator.async_write_device_settings = AsyncMock()  # type: ignore[method-assign]

    await entity.async_set_hvac_mode(HVACMode.COOL)
    await entity.async_set_temperature(**{ATTR_TEMPERATURE: 69})
    await entity.async_set_temperature(
        **{ATTR_HVAC_MODE: HVACMode.COOL, ATTR_TEMPERATURE: 73}
    )
    await entity.async_set_temperature(
        **{
            ATTR_HVAC_MODE: HVACMode.HEAT_COOL,
            ATTR_TARGET_TEMP_LOW: 66,
            ATTR_TARGET_TEMP_HIGH: 74,
        }
    )
    await entity.async_set_fan_mode("circulate")
    await entity.async_set_preset_mode("vacation")

    assert coordinator.async_write_device_settings.await_args_list == [
        call(device_id, {"thermostatPZ1": {"mode": "cool"}}),
        call(device_id, {"thermostatPZ1": {"heatSetpoint": 69}}),
        call(device_id, {"thermostatPZ1": {"coolSetpoint": 73}}),
        call(device_id, {"thermostatPZ1": {"heatSetpoint": 66, "coolSetpoint": 74}}),
        call(device_id, {"thermostatPZ1": {"fan": "circulate"}}),
        call(device_id, {"thermostatPZ1": {"holdType": "vacation"}}),
    ]


async def test_thermostat_emergency_heat_is_read_as_heat_with_raw_attribute(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Emergency heat should be safe to read without adding a write mode."""
    coordinator = await _bootstrap_thermostat_coordinator(hass, monkeypatch)
    device_id = coordinator.data.supported_device_ids[0]
    device = coordinator.data.devices[device_id]
    settings = build_thermostat_settings()
    settings["thermostatPZ1"]["mode"] = "emergency-heat"
    coordinator._devices[device_id] = replace(device, device_settings=settings)
    coordinator.async_set_updated_data(coordinator._build_snapshot())

    entity = AprilaireThermostatClimateEntity(coordinator, device_id, "thermostat_pz1")

    assert entity.hvac_mode is HVACMode.HEAT
    assert entity.extra_state_attributes["raw_hvac_mode"] == "emergency-heat"


async def test_thermostat_iaq_sensors_appear_only_when_status_data_exists(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Thermostat dynamic sensors should be zone-aware and IAQ-status gated."""
    coordinator = await _bootstrap_thermostat_coordinator(hass, monkeypatch)
    device_id = coordinator.data.supported_device_ids[0]
    profile = coordinator.data.devices[device_id].profile_key
    assert profile == "thermostat"

    entity_set = coordinator.data.devices[device_id]
    descriptions = entity_set.status_payloads
    assert "iaq_humidifier" in descriptions
    assert "iaq_aircleaning" in descriptions
    assert "iaq_dehumidifier" not in descriptions

    zone_sensor = AprilaireThermostatZoneSensor(
        coordinator,
        device_id,
        "PZ1",
        "outdoor_temperature",
        "thermostat_pz1_outdoor_temperature",
    )
    iaq_sensor = AprilaireThermostatIAQSensor(
        coordinator,
        device_id,
        "humidifier",
        "status",
        "iaq_humidifier_status",
    )

    assert zone_sensor.unique_id == f"{device_id}_thermostat_pz1_outdoor_temperature"
    assert zone_sensor.native_value == 35
    assert iaq_sensor.unique_id == f"{device_id}_iaq_humidifier_status"
    assert iaq_sensor.native_value == "humidifying"
