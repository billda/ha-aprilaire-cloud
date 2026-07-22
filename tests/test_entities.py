"""Entity behavior tests for AprilAire Cloud."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, call

import pytest
from homeassistant.components.climate import ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.components.climate.const import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
)
from homeassistant.components.humidifier import HumidifierAction
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aprilaire_cloud.binary_sensor import (
    DEHUMIDIFIER_BINARY_SENSORS,
    AprilaireAttachedHumidifierServiceEntity,
    AprilaireBinarySensorEntity,
    AprilaireWebSocketStatusEntity,
)
from custom_components.aprilaire_cloud.climate import AprilaireThermostatClimateEntity
from custom_components.aprilaire_cloud.const import CONF_ENABLE_EXTRA_DIAGNOSTICS, DOMAIN
from custom_components.aprilaire_cloud.coordinator import AprilaireCloudDataUpdateCoordinator
from custom_components.aprilaire_cloud.entity import raise_ha_write_error
from custom_components.aprilaire_cloud.humidifier import (
    AprilaireAttachedHumidifierEntity,
    AprilaireCloudHumidifierEntity,
)
from custom_components.aprilaire_cloud.models import SocketState
from custom_components.aprilaire_cloud.number import AprilaireAlertLimitNumber
from custom_components.aprilaire_cloud.profiles import (
    SetAttachedHumidifierPower,
    SetAttachedHumidifierTarget,
    SetHighHumidityAlert,
    SetThermostatFan,
    SetThermostatHold,
    SetThermostatMode,
    SetThermostatSetpoints,
)
from custom_components.aprilaire_cloud.sensor import (
    DEHUMIDIFIER_SENSORS,
    AprilaireAttachedHumidifierSensor,
    AprilaireDehumidifierExtraTemperatureSensor,
    AprilaireStaticSensorEntity,
    AprilaireThermostatIAQSensor,
    AprilaireThermostatZoneSensor,
    _create_dynamic_sensor,
    _dehumidifier_dynamic_sensor,
    _thermostat_dynamic_sensor,
)
from custom_components.aprilaire_cloud.switch import (
    AprilaireDehumidifierPowerSwitch,
)
from custom_components.aprilaire_cloud.vendor import (
    AprilaireCloudRateLimitError,
    AprilaireCloudWriteError,
)

from .common import (
    DEVICE_ID,
    LOCATION_ID,
    PASSWORD,
    SECOND_DEVICE_ID,
    SECOND_LOCATION_ID,
    USERNAME,
    FakeClient,
    FakeWebSocket,
    MultiLocationFakeWebSocket,
    ThermostatFakeWebSocket,
    bootstrap_coordinator,
    build_iaq_status,
    build_thermostat_hierarchy,
    build_thermostat_settings,
    build_two_location_hierarchy,
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
    alert_limit = AprilaireAlertLimitNumber(coordinator, device_id, "high_humidity")

    assert humidifier.target_humidity == 60
    assert humidifier.is_on is False
    assert alert_limit.native_value == 70
    assert alert_limit.entity_category.value == "config"


async def test_dehumidifier_control_adapters_emit_typed_commands(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Humidifier, switch, and number adapters emit intent rather than JSON."""
    client = FakeClient()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket",
        FakeWebSocket,
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=build_user()["userId"],
        data={"username": USERNAME, "password": PASSWORD},
    )
    entry.add_to_hass(hass)
    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass,
        config_entry=entry,
        client=client,
    )
    await bootstrap_coordinator(coordinator)
    humidifier = AprilaireCloudHumidifierEntity(coordinator, DEVICE_ID)
    power = AprilaireDehumidifierPowerSwitch(coordinator, DEVICE_ID)
    number = AprilaireAlertLimitNumber(
        coordinator,
        DEVICE_ID,
        "high_humidity",
    )
    coordinator.async_execute_command = AsyncMock()  # type: ignore[method-assign]

    assert power.is_on is True
    await humidifier.async_turn_on()
    await humidifier.async_turn_off()
    await humidifier.async_set_humidity(55)
    await power.async_turn_off()
    await power.async_turn_on()
    await number.async_set_native_value(70)

    assert coordinator.async_execute_command.await_count == 6
    assert coordinator.async_execute_command.await_args_list[-1] == call(
        DEVICE_ID,
        SetHighHumidityAlert(humidity=70),
    )


async def test_control_adapter_failures_are_translated(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Every adapter converts typed vendor failures to an HA write error."""
    client = FakeClient()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket",
        FakeWebSocket,
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=build_user()["userId"],
        data={"username": USERNAME, "password": PASSWORD},
    )
    entry.add_to_hass(hass)
    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass,
        config_entry=entry,
        client=client,
    )
    await bootstrap_coordinator(coordinator)
    coordinator.async_execute_command = AsyncMock(  # type: ignore[method-assign]
        side_effect=AprilaireCloudWriteError("sanitized")
    )

    with pytest.raises(HomeAssistantError):
        await AprilaireCloudHumidifierEntity(
            coordinator,
            DEVICE_ID,
        ).async_turn_on()
    with pytest.raises(HomeAssistantError):
        await AprilaireDehumidifierPowerSwitch(
            coordinator,
            DEVICE_ID,
        ).async_turn_off()
    with pytest.raises(HomeAssistantError):
        await AprilaireAlertLimitNumber(
            coordinator,
            DEVICE_ID,
            "high_humidity",
        ).async_set_native_value(70)


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
    hass.config.units = US_CUSTOMARY_SYSTEM
    pz1.hass = hass

    assert pz1.unique_id == f"{device_id}_thermostat_pz1"
    assert pz1.temperature_unit == "°C"
    assert pz1.current_temperature == 21
    assert pz1.state_attributes[ATTR_CURRENT_TEMPERATURE] == 70
    assert pz1.current_humidity == 45
    assert pz1.hvac_mode is HVACMode.HEAT
    assert pz1.hvac_action is HVACAction.HEATING
    assert pz1.target_temperature == 20
    assert pz1.target_temperature_low == 20
    assert pz1.target_temperature_high == 24
    assert pz1.fan_mode == "auto"
    assert pz1.preset_mode == "permanent"
    assert ClimateEntityFeature.TARGET_TEMPERATURE not in pz1.supported_features
    assert ClimateEntityFeature.FAN_MODE in pz1.supported_features
    assert ClimateEntityFeature.PRESET_MODE in pz1.supported_features

    assert sz2.unique_id == f"{device_id}_thermostat_sz2"
    assert sz2.hvac_mode is HVACMode.HEAT_COOL
    assert sz2.fan_mode == "circulate"
    assert sz2.preset_mode == "temporary"


async def test_thermostat_action_uses_separate_status_fields_in_priority_order(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Heating, cooling, fan, then explicit idle determine HVAC action."""
    coordinator = await _bootstrap_thermostat_coordinator(hass, monkeypatch)
    device_id = coordinator.data.supported_device_ids[0]
    entity = AprilaireThermostatClimateEntity(
        coordinator,
        device_id,
        "thermostat_pz1",
    )
    equipment = AprilaireThermostatZoneSensor(
        coordinator,
        device_id,
        "PZ1",
        "equipment_status",
        "thermostat_pz1_equipment_status",
    )

    await coordinator.async_process_messages(
        LOCATION_ID,
        [
            {
                "_type": "ThermostatStatus",
                "deviceId": device_id,
                "zone": "PZ1",
                "asOf": "2026-03-24T00:10:00.000Z",
                "heatingStatus": "heating",
                "coolingStatus": "cooling",
                "isFanOn": True,
                "equipmentStatus": "unknown-vendor-state",
            }
        ],
    )
    assert entity.hvac_action is HVACAction.HEATING

    await coordinator.async_process_messages(
        LOCATION_ID,
        [
            {
                "_type": "ThermostatStatus",
                "deviceId": device_id,
                "zone": "PZ1",
                "asOf": "2026-03-24T00:11:00.000Z",
                "heatingStatus": "idle",
                "coolingStatus": "stage1",
                "isFanOn": True,
            }
        ],
    )
    assert entity.hvac_action is HVACAction.COOLING
    assert equipment.native_value == "cooling"

    await coordinator.async_process_messages(
        LOCATION_ID,
        [
            {
                "_type": "ThermostatStatus",
                "deviceId": device_id,
                "zone": "PZ1",
                "asOf": "2026-03-24T00:12:00.000Z",
                "heatingStatus": "idle",
                "coolingStatus": "idle",
                "isFanOn": True,
            }
        ],
    )
    assert entity.hvac_action is HVACAction.FAN

    await coordinator.async_process_messages(
        LOCATION_ID,
        [
            {
                "_type": "ThermostatStatus",
                "deviceId": device_id,
                "zone": "PZ1",
                "asOf": "2026-03-24T00:13:00.000Z",
                "heatingStatus": "idle",
                "coolingStatus": "inactive",
                "isFanOn": False,
            }
        ],
    )
    assert entity.hvac_action is HVACAction.IDLE


async def test_climate_adapter_rejects_invalid_services_and_handles_missing_zone(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Invalid HA service values fail early and absent normalized state stays None."""
    coordinator = await _bootstrap_thermostat_coordinator(hass, monkeypatch)
    device_id = coordinator.data.supported_device_ids[0]
    entity = AprilaireThermostatClimateEntity(
        coordinator,
        device_id,
        "thermostat_pz1",
    )
    missing = AprilaireThermostatClimateEntity(
        coordinator,
        device_id,
        "thermostat_unknown",
    )

    assert missing.current_temperature is None
    assert missing.current_humidity is None
    assert missing.hvac_mode is None
    assert missing.hvac_action is None
    assert missing.target_temperature is None
    assert missing.target_temperature_low is None
    assert missing.target_temperature_high is None
    assert missing.fan_mode is None
    assert missing.preset_mode is None
    assert missing.extra_state_attributes == {}
    with pytest.raises(HomeAssistantError):
        await entity.async_set_hvac_mode("invalid")  # type: ignore[arg-type]
    with pytest.raises(HomeAssistantError):
        await entity.async_set_fan_mode("invalid")
    with pytest.raises(HomeAssistantError):
        await entity.async_set_preset_mode("invalid")
    with pytest.raises(HomeAssistantError):
        await entity.async_set_temperature()

    coordinator.async_execute_command = AsyncMock(  # type: ignore[method-assign]
        side_effect=AprilaireCloudWriteError("sanitized")
    )
    with pytest.raises(HomeAssistantError):
        await entity.async_set_hvac_mode(HVACMode.HEAT)


async def test_thermostat_climate_writes_exact_patch_payloads(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Thermostat climate writes should patch only the owning zone settings key."""
    coordinator = await _bootstrap_thermostat_coordinator(hass, monkeypatch)
    device_id = coordinator.data.supported_device_ids[0]
    entity = AprilaireThermostatClimateEntity(coordinator, device_id, "thermostat_pz1")
    coordinator.async_execute_command = AsyncMock()  # type: ignore[method-assign]

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

    assert coordinator.async_execute_command.await_args_list == [
        call(device_id, SetThermostatMode(zone="PZ1", mode="cool")),
        call(device_id, SetThermostatSetpoints(zone="PZ1", heat=69.0)),
        call(device_id, SetThermostatSetpoints(zone="PZ1", cool=73.0)),
        call(device_id, SetThermostatSetpoints(zone="PZ1", heat=66.0, cool=74.0)),
        call(device_id, SetThermostatFan(zone="PZ1", mode="circulate")),
        call(device_id, SetThermostatHold(zone="PZ1", hold="vacation")),
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
    assert zone_sensor.native_value == 2
    assert iaq_sensor.unique_id == f"{device_id}_iaq_humidifier_status"
    assert iaq_sensor.native_value == "humidifying"


async def test_attached_humidifier_is_global_and_uses_reported_service_values(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Explicit installation creates one global entity with no first-zone binding."""
    coordinator = await _bootstrap_thermostat_coordinator(hass, monkeypatch)
    device_id = coordinator.data.supported_device_ids[0]
    settings = build_thermostat_settings()
    settings["asOf"] = "2026-03-24T00:10:00.000Z"
    settings["humidifier"] = {"mode": "on", "humiditySetpoint": 40}
    status = build_iaq_status(
        message_type="HumidifierStatus",
        status="humidifying",
    )
    status["asOf"] = "2026-03-24T00:11:00.000Z"
    status.pop("filterService")
    status["currentHumidity"] = 38
    status["waterPanelService"] = {"needsService": True, "remaining": 17}
    await coordinator.async_process_messages(LOCATION_ID, [settings, status])

    humidifier = AprilaireAttachedHumidifierEntity(coordinator, device_id)
    remaining = AprilaireAttachedHumidifierSensor(
        coordinator,
        device_id,
        "attached_humidifier_water_panel_remaining",
    )
    service = AprilaireAttachedHumidifierServiceEntity(coordinator, device_id)
    coordinator.async_execute_command = AsyncMock()  # type: ignore[method-assign]

    assert humidifier.unique_id == f"{device_id}_attached_humidifier"
    assert "pz1" not in humidifier.unique_id
    assert humidifier.current_humidity == 38
    assert humidifier.target_humidity == 40
    assert humidifier.max_humidity == 50
    assert humidifier.is_on is True
    assert humidifier.action is HumidifierAction.HUMIDIFYING
    assert remaining.native_value == 17
    assert service.is_on is True
    assert humidifier.extra_state_attributes == {
        "equipment_status": "humidifying",
        "water_panel_remaining": 17,
        "water_panel_needs_service": True,
    }

    await humidifier.async_turn_on()
    await humidifier.async_turn_off()
    await humidifier.async_set_humidity(42)
    assert coordinator.async_execute_command.await_args_list == [
        call(device_id, SetAttachedHumidifierPower(enabled=True)),
        call(device_id, SetAttachedHumidifierPower(enabled=False)),
        call(device_id, SetAttachedHumidifierTarget(humidity=42)),
    ]


async def test_offline_and_rescinded_events_change_child_availability(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """An explicit device event affects only entity availability, not identity."""
    coordinator = await _bootstrap_thermostat_coordinator(hass, monkeypatch)
    device_id = coordinator.data.supported_device_ids[0]
    entity = AprilaireThermostatClimateEntity(
        coordinator,
        device_id,
        "thermostat_pz1",
    )
    unique_id = entity.unique_id

    await coordinator.async_process_messages(
        LOCATION_ID,
        [
            {
                "_type": "DeviceEvent",
                "deviceId": device_id,
                "type": "offline",
                "occurred": "2026-03-24T00:10:00.000Z",
            }
        ],
    )
    assert entity.available is False
    assert entity.unique_id == unique_id

    await coordinator.async_process_messages(
        LOCATION_ID,
        [
            {
                "_type": "DeviceEvent",
                "deviceId": device_id,
                "type": "offline",
                "occurred": "2026-03-24T00:10:00.000Z",
                "rescinded": "2026-03-24T00:11:00.000Z",
            }
        ],
    )
    assert entity.available is True
    assert entity.unique_id == unique_id


async def test_hierarchy_failure_does_not_hide_device_with_healthy_push(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Coordinator-global failure cannot override a healthy per-device source."""
    coordinator = await _bootstrap_thermostat_coordinator(hass, monkeypatch)
    device_id = coordinator.data.supported_device_ids[0]
    entity = AprilaireThermostatClimateEntity(
        coordinator,
        device_id,
        "thermostat_pz1",
    )

    coordinator.last_update_success = False

    assert coordinator.data.socket_states[LOCATION_ID].initial_sync_complete is True
    assert entity.available is True


async def test_rest_success_keeps_device_available_when_websocket_fails(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Fresh device REST data is a healthy source independent of WebSocket state."""
    coordinator = await _bootstrap_thermostat_coordinator(hass, monkeypatch)
    device_id = coordinator.data.supported_device_ids[0]
    entity = AprilaireThermostatClimateEntity(
        coordinator,
        device_id,
        "thermostat_pz1",
    )
    await coordinator.async_socket_state_changed(
        SocketState(
            location_id=LOCATION_ID,
            connected=False,
            initial_sync_complete=False,
            last_error="transport_unavailable",
        )
    )

    refreshed, errors = await coordinator._async_rest_refresh_devices({device_id})
    coordinator._publish_snapshot()

    assert refreshed == {device_id}
    assert errors == {}
    assert coordinator.data.devices[device_id].health.last_rest_received_at is not None
    assert entity.available is True


async def test_explicit_offline_event_affects_only_its_device(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """One device event cannot make a second healthy device unavailable."""
    client = FakeClient()
    client._hierarchy = build_two_location_hierarchy()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket",
        MultiLocationFakeWebSocket,
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=build_user()["userId"],
        data={"username": USERNAME, "password": PASSWORD},
    )
    entry.add_to_hass(hass)
    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass,
        config_entry=entry,
        client=client,
    )
    await bootstrap_coordinator(coordinator)
    first = AprilaireCloudHumidifierEntity(coordinator, DEVICE_ID)
    second = AprilaireCloudHumidifierEntity(coordinator, SECOND_DEVICE_ID)

    await coordinator.async_process_messages(
        LOCATION_ID,
        [
            {
                "_type": "DeviceEvent",
                "deviceId": DEVICE_ID,
                "type": "offline",
                "occurred": "2026-03-24T00:10:00.000Z",
            }
        ],
    )

    assert first.available is False
    assert second.available is True
    assert coordinator.data.socket_states[SECOND_LOCATION_ID].initial_sync_complete is True


async def test_sensor_and_binary_adapter_edge_paths(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Dynamic factories and optional-value adapters handle absence honestly."""
    client = FakeClient()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket",
        FakeWebSocket,
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=build_user()["userId"],
        data={"username": USERNAME, "password": PASSWORD},
    )
    entry.add_to_hass(hass)
    coordinator = AprilaireCloudDataUpdateCoordinator(
        hass,
        config_entry=entry,
        client=client,
    )
    await bootstrap_coordinator(coordinator)

    assert _dehumidifier_dynamic_sensor(
        coordinator,
        DEVICE_ID,
        "not-temperature",
    ) is None
    assert _dehumidifier_dynamic_sensor(
        coordinator,
        DEVICE_ID,
        "temperature_bad",
    ) is None
    with pytest.raises(ValueError):
        _create_dynamic_sensor(
            lambda coordinator, device_id, key: None,
            coordinator,
            DEVICE_ID,
            "unsupported",
        )
    probe = AprilaireDehumidifierExtraTemperatureSensor(
        coordinator,
        DEVICE_ID,
        4,
    )
    missing_probe = AprilaireDehumidifierExtraTemperatureSensor(
        coordinator,
        DEVICE_ID,
        99,
    )
    assert probe.name == "Sensor Four"
    assert probe.native_value == 16.21
    assert missing_probe.name == "Temperature 99"
    assert missing_probe.native_value is None
    assert AprilaireStaticSensorEntity(
        coordinator,
        "missing-device",
        DEHUMIDIFIER_SENSORS["current_humidity"],
    ).native_value is None
    assert AprilaireBinarySensorEntity(
        coordinator,
        "missing-device",
        DEHUMIDIFIER_BINARY_SENSORS["filter_service"],
    ).is_on is None

    websocket = AprilaireWebSocketStatusEntity(coordinator, "missing-location")
    assert websocket.is_on is None
    assert websocket.available is False
    assert websocket.extra_state_attributes == {}
    assert websocket.device_info is None


async def test_thermostat_dynamic_sensor_metric_paths(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Every normalized thermostat metric adapter returns its owned field."""
    coordinator = await _bootstrap_thermostat_coordinator(hass, monkeypatch)
    device_id = coordinator.data.supported_device_ids[0]

    assert _thermostat_dynamic_sensor(
        coordinator,
        device_id,
        "thermostat_bad",
    ) is None
    assert _thermostat_dynamic_sensor(
        coordinator,
        device_id,
        "thermostat_pz1_unknown",
    ) is None
    assert _thermostat_dynamic_sensor(
        coordinator,
        device_id,
        "unknown",
    ) is None
    assert isinstance(
        _thermostat_dynamic_sensor(
            coordinator,
            device_id,
            "attached_humidifier_water_panel_remaining",
        ),
        AprilaireAttachedHumidifierSensor,
    )

    expected = {
        "indoor_temperature": 21,
        "indoor_humidity": 45,
        "heat_setpoint": 20,
        "cool_setpoint": 24,
        "outdoor_temperature": 2,
        "outdoor_humidity": 62,
        "equipment_status": "heating",
        "hvac_service_remaining": 88,
    }
    for metric, value in expected.items():
        sensor = AprilaireThermostatZoneSensor(
            coordinator,
            device_id,
            "PZ1",
            metric,
            f"thermostat_pz1_{metric}",
        )
        assert sensor.native_value == value
        if metric in {
            "indoor_temperature",
            "heat_setpoint",
            "cool_setpoint",
            "outdoor_temperature",
        }:
            assert sensor.native_unit_of_measurement == "°C"

    missing = AprilaireThermostatZoneSensor(
        coordinator,
        device_id,
        "UNKNOWN",
        "equipment_status",
        "thermostat_unknown_equipment_status",
    )
    assert missing.native_value is None
    iaq_service = AprilaireThermostatIAQSensor(
        coordinator,
        device_id,
        "humidifier",
        "service_remaining",
        "iaq_humidifier_service_remaining",
    )
    assert iaq_service.native_value == 80
    assert AprilaireThermostatIAQSensor(
        coordinator,
        device_id,
        "unknown",
        "status",
        "iaq_unknown_status",
    ).native_value is None
