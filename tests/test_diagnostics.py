"""Diagnostics privacy tests for AprilAire Cloud."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aprilaire_cloud.const import DOMAIN
from custom_components.aprilaire_cloud.coordinator import AprilaireCloudDataUpdateCoordinator
from custom_components.aprilaire_cloud.data import AprilaireCloudRuntimeData
from custom_components.aprilaire_cloud.diagnostics import (
    ExportPseudonymizer,
    async_get_config_entry_diagnostics,
)
from custom_components.aprilaire_cloud.state import DeviceWriteState

from .common import (
    DEVICE_ID,
    LOCATION_ID,
    PASSWORD,
    THERMOSTAT_DEVICE_ID,
    USERNAME,
    FakeClient,
    FakeWebSocket,
    ThermostatFakeWebSocket,
    bootstrap_coordinator,
    build_thermostat_hierarchy,
    build_thermostat_settings,
    build_user,
)


def _entry(hass) -> MockConfigEntry:
    """Create and register a synthetic config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=build_user()["userId"],
        data={"username": USERNAME, "password": PASSWORD},
    )
    entry.add_to_hass(hass)
    return entry


async def test_diagnostics_are_value_free_and_export_scoped(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """A complete default export must contain no known private fixture values."""
    client = FakeClient()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket
    )
    entry = _entry(hass)
    coordinator = AprilaireCloudDataUpdateCoordinator(hass, config_entry=entry, client=client)
    await bootstrap_coordinator(coordinator)

    device = coordinator.data.devices[DEVICE_ID]
    coordinator._devices[DEVICE_ID] = replace(
        device,
        pending_device_settings={"dehumidifier": {"humiditySetpoint": 60}},
    )
    write_state = DeviceWriteState(
        pending_paths=("dehumidifier.humiditySetpoint",),
        inflight_paths=("dehumidifier.humiditySetpoint",),
    )
    write_state.inflight_expected = {"dehumidifier": {"humiditySetpoint": 60}}
    write_state.last_confirmed_settings = {"dehumidifier": {"humiditySetpoint": 52}}
    write_state.inflight_event = asyncio.Event()
    coordinator._write_states[DEVICE_ID] = write_state
    coordinator.async_set_updated_data(coordinator._build_snapshot())
    entry.runtime_data = AprilaireCloudRuntimeData(
        client=client,
        coordinator=coordinator,
        integration=None,  # type: ignore[arg-type]
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    serialized = json.dumps(diagnostics, sort_keys=True)

    assert diagnostics["privacy"] == {
        "safe_for_public_issue": True,
        "review_before_sharing": True,
        "pseudonyms_are_export_scoped": True,
        "raw_vendor_payloads_included": False,
    }
    assert set(diagnostics["snapshot"]["locations"]) == {"location_1"}
    assert set(diagnostics["snapshot"]["devices"]) == {"device_1"}
    assert diagnostics["snapshot"]["devices"]["device_1"]["location"] == "location_1"
    assert diagnostics["snapshot"]["write_states"]["device_1"] == {
        "pending_paths": ["dehumidifier.humiditySetpoint"],
        "inflight_paths": ["dehumidifier.humiditySetpoint"],
        "waiting_for_confirmation": True,
    }
    assert diagnostics["snapshot"]["devices"]["device_1"]["model"] == "E100W"
    assert "payload_shapes" in diagnostics["snapshot"]["devices"]["device_1"]

    forbidden_values = {
        USERNAME,
        PASSWORD,
        build_user()["userId"],
        DEVICE_ID,
        LOCATION_ID,
        entry.entry_id,
        "Synthetic Home",
        "Utility Room",
        "Sensor One",
        f"humidifier.{DEVICE_ID}_dehumidifier",
        "52",
        "60",
    }
    assert all(value not in serialized for value in forbidden_values)


async def test_diagnostics_include_value_free_thermostat_shape(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Thermostat diagnostics retain schema presence without raw state values."""
    client = FakeClient()
    client._hierarchy = build_thermostat_hierarchy()
    client.device_settings = build_thermostat_settings()
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket",
        ThermostatFakeWebSocket,
    )
    entry = _entry(hass)
    coordinator = AprilaireCloudDataUpdateCoordinator(hass, config_entry=entry, client=client)
    await bootstrap_coordinator(coordinator)
    entry.runtime_data = AprilaireCloudRuntimeData(
        client=client,
        coordinator=coordinator,
        integration=None,  # type: ignore[arg-type]
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    profile = diagnostics["snapshot"]["devices"]["device_1"]["profile_diagnostics"]
    serialized = json.dumps(diagnostics, sort_keys=True)

    assert profile["status_payload_keys"] == [
        "iaq_aircleaning",
        "iaq_humidifier",
        "thermostatPZ1",
        "thermostatSZ2",
        "thermostatSZ3",
    ]
    assert profile["capability_names"] == [
        "attached_humidifier_power",
        "attached_humidifier_target",
        "thermostat_fan",
        "thermostat_hold",
        "thermostat_mode",
        "thermostat_setpoints",
    ]
    assert profile["thermostat"]["zones"]["PZ1"]["has_current_temperature"] is True
    assert THERMOSTAT_DEVICE_ID not in serialized
    assert '"raw_mode"' not in serialized
    settings_shape = diagnostics["snapshot"]["devices"]["device_1"]["payload_shapes"][
        "device_settings"
    ]
    assert settings_shape["thermostatPZ1"]["heatSetpoint"] == "int"


def test_pseudonymizer_preserves_references_without_stable_hashes() -> None:
    """Labels are consistent within an export and reveal no source material."""
    pseudonyms = ExportPseudonymizer()

    assert pseudonyms.label("device", "private-a") == "device_1"
    assert pseudonyms.label("device", "private-a") == "device_1"
    assert pseudonyms.label("device", "private-b") == "device_2"
    assert pseudonyms.label("location", "private-a") == "location_1"
    assert pseudonyms.label("registry_device", "private-a") == "registry_device_1"
    assert pseudonyms.label("device", None) is None
