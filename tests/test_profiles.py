"""Capability and command-codec contracts for device profiles."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from custom_components.aprilaire_cloud.models import DeviceRecord
from custom_components.aprilaire_cloud.profiles import (
    CommandAccessError,
    CommandNotSupportedError,
    CommandType,
    CommandValidationError,
    EvidenceLevel,
    NormalizedThermostatState,
    SetAttachedHumidifierPower,
    SetAttachedHumidifierTarget,
    SetDehumidifierPower,
    SetDehumidifierTarget,
    SetHighHumidityAlert,
    SetThermostatFan,
    SetThermostatHold,
    SetThermostatMode,
    SetThermostatSetpoints,
    get_profile,
)
from custom_components.aprilaire_cloud.state import (
    apply_device_message,
    apply_hierarchy,
    evaluate_device_support,
)

from .common import (
    DEVICE_ID,
    THERMOSTAT_DEVICE_ID,
    build_device_status,
    build_hierarchy,
    build_initial_messages,
    build_thermostat_hierarchy,
    build_thermostat_initial_messages,
)

FIXTURES = Path(__file__).with_name("fixtures")


def _fixture(name: str):
    """Load one sanitized protocol fixture."""
    return json.loads((FIXTURES / name).read_text())


def _record(
    hierarchy: dict,
    device_id: str,
    messages: list[dict],
) -> DeviceRecord:
    """Build a recognized record from synthetic protocol evidence."""
    _, devices, _ = apply_hierarchy(hierarchy, {})
    record = devices[device_id]
    for message in messages:
        record = evaluate_device_support(apply_device_message(record, message))
    assert record.supported
    return record


def _dehumidifier() -> DeviceRecord:
    """Return a fully hydrated E100W-compatible record."""
    return _record(build_hierarchy(), DEVICE_ID, build_initial_messages())


def _thermostat() -> DeviceRecord:
    """Return a fully hydrated 8920W-compatible record."""
    return _record(
        build_thermostat_hierarchy(),
        THERMOSTAT_DEVICE_ID,
        build_thermostat_initial_messages(),
    )


def test_read_only_access_never_encodes_a_patch() -> None:
    """A shared/read hierarchy cannot acquire write access from payload shape."""
    record = _dehumidifier()
    record = replace(
        record,
        hierarchy=replace(record.hierarchy, access="read"),
    )
    profile = get_profile(record.profile_key)
    assert profile is not None

    capabilities = profile.capabilities(record)
    assert all(not capability.writable for capability in capabilities.commands.values())
    assert {
        capability.unavailable_reason for capability in capabilities.commands.values()
    } == {"account_access_read_only"}
    with pytest.raises(CommandAccessError):
        profile.encode_command(record, SetDehumidifierPower(enabled=False))


def test_manage_access_allows_only_profile_declared_commands() -> None:
    """Manage is necessary but does not bypass family capability declarations."""
    record = _dehumidifier()
    profile = get_profile(record.profile_key)
    assert profile is not None

    assert profile.encode_command(
        record, SetDehumidifierPower(enabled=False)
    ).payload == {"dehumidifier": {"mode": "off"}}
    assert profile.encode_command(
        record, SetDehumidifierTarget(humidity=55)
    ).payload == {"dehumidifier": {"humiditySetpoint": 55}}
    assert profile.encode_command(
        record, SetHighHumidityAlert(humidity=70)
    ).payload == {"dehumidifier": {"alertLimits": {"highHum": 70}}}
    with pytest.raises(CommandNotSupportedError):
        profile.encode_command(
            record,
            SetThermostatMode(zone="PZ1", mode="heat"),
        )


def test_command_constraints_are_validated_by_the_profile() -> None:
    """Out-of-range numeric intent fails before a vendor payload is returned."""
    record = _dehumidifier()
    profile = get_profile(record.profile_key)
    assert profile is not None

    with pytest.raises(CommandValidationError):
        profile.encode_command(record, SetDehumidifierTarget(humidity=39))
    with pytest.raises(CommandValidationError):
        profile.encode_command(record, SetHighHumidityAlert(humidity=91))


def test_capability_entities_survive_partial_falsy_frames() -> None:
    """A partial frame preserves stable entity descriptors and falsy values."""
    record = _dehumidifier()
    profile = get_profile(record.profile_key)
    assert profile is not None
    before = profile.entity_descriptions(record)

    record = evaluate_device_support(
        apply_device_message(
            record,
            {
                "_type": "DeviceSettings",
                "deviceId": DEVICE_ID,
                "asOf": "2026-03-24T00:10:00.000Z",
                "dehumidifier": {"mode": "off", "forceHvacFan": False},
            },
        )
    )

    assert profile.entity_descriptions(record) == before
    assert record.device_settings["dehumidifier"]["mode"] == "off"
    assert record.device_settings["dehumidifier"]["forceHvacFan"] is False


def test_external_control_exposes_sensors_and_on_off_without_target() -> None:
    """External control keeps honest telemetry and the confirmed power contract."""
    record = _record(
        build_hierarchy(),
        DEVICE_ID,
        build_initial_messages(control_type="external"),
    )
    profile = get_profile(record.profile_key)
    assert profile is not None
    capabilities = profile.capabilities(record)

    assert capabilities.entities.humidifier_keys == ()
    assert capabilities.entities.switch_keys == ("dehumidifier_power",)
    assert {"current_humidity", "current_temperature"}.issubset(
        capabilities.entities.sensor_keys
    )
    assert capabilities.commands[CommandType.DEHUMIDIFIER_POWER].writable is True
    assert capabilities.commands[CommandType.DEHUMIDIFIER_TARGET].writable is False
    with pytest.raises(CommandNotSupportedError):
        profile.encode_command(record, SetDehumidifierTarget(humidity=55))


def test_read_only_device_has_telemetry_but_no_control_entity() -> None:
    """Read access does not expose a UI control that can only fail."""
    record = _dehumidifier()
    record = replace(
        record,
        hierarchy=replace(record.hierarchy, access="read"),
    )
    profile = get_profile(record.profile_key)
    assert profile is not None
    entities = profile.entity_descriptions(record)

    assert entities.humidifier_keys == ()
    assert entities.switch_keys == ()
    assert entities.number_keys == ()
    assert entities.sensor_keys


def test_sensor_selection_is_deterministic_for_partial_control() -> None:
    """Controlling, reporting-primary, then first valid is the fixed priority."""
    messages = build_initial_messages(control_type="external")
    status = messages[0]
    status["humSensors"] = [
        {"uid": 1, "reading": 41, "status": "reporting"},
        {
            "uid": 2,
            "reading": 42,
            "status": "reporting",
            "isPrimary": True,
        },
        {"uid": 3, "reading": 43, "isControlling": True},
    ]
    record = _record(build_hierarchy(), DEVICE_ID, messages)
    profile = get_profile(record.profile_key)
    assert profile is not None

    assert profile.normalize(record).current_humidity == 43  # type: ignore[union-attr]
    status["humSensors"][2].pop("isControlling")
    record = _record(build_hierarchy(), DEVICE_ID, messages)
    assert profile.normalize(record).current_humidity == 42  # type: ignore[union-attr]
    status["humSensors"][1].pop("isPrimary")
    record = _record(build_hierarchy(), DEVICE_ID, messages)
    assert profile.normalize(record).current_humidity == 41  # type: ignore[union-attr]


def test_thermostat_codecs_use_only_observed_zone_keys() -> None:
    """The 8920W codec preserves the settings contract of the selected zone."""
    record = _thermostat()
    profile = get_profile(record.profile_key)
    assert profile is not None

    assert profile.encode_command(
        record,
        SetThermostatMode(zone="PZ1", mode="cool"),
    ).payload == {"thermostatPZ1": {"mode": "cool"}}
    assert profile.encode_command(
        record,
        SetThermostatMode(zone="SZ2", mode="heat"),
    ).payload == {"thermostatSZ2": {"ModeId": 2}}
    with pytest.raises(CommandNotSupportedError):
        profile.encode_command(
            record,
            SetThermostatMode(zone="not-a-zone", mode="heat"),
        )
    assert profile.encode_command(
        record,
        SetThermostatFan(zone="PZ1", mode="circulate"),
    ).payload == {"thermostatPZ1": {"fan": "circulate"}}
    assert profile.encode_command(
        record,
        SetThermostatFan(zone="SZ2", mode="on"),
    ).payload == {"thermostatSZ2": {"FanId": 2}}
    assert profile.encode_command(
        record,
        SetThermostatHold(zone="PZ1", hold="vacation"),
    ).payload == {"thermostatPZ1": {"holdType": "vacation"}}
    assert profile.encode_command(
        record,
        SetThermostatHold(zone="SZ2", hold="none"),
    ).payload == {"thermostatSZ2": {"HoldType": 0}}
    with pytest.raises(CommandValidationError):
        profile.encode_command(
            record,
            SetThermostatFan(zone="PZ1", mode="invented"),
        )
    with pytest.raises(CommandValidationError):
        profile.encode_command(
            record,
            SetThermostatHold(zone="PZ1", hold="invented"),
        )


def test_unproven_thermostat_temperature_contract_stays_read_only() -> None:
    """Setpoint PATCHes remain disabled without explicit unit and constraints."""
    record = _thermostat()
    profile = get_profile(record.profile_key)
    assert profile is not None
    capability = profile.capabilities(record).commands[
        CommandType.THERMOSTAT_SETPOINTS
    ]

    assert capability.writable is False
    assert capability.evidence is EvidenceLevel.UNKNOWN
    assert capability.unavailable_reason == "temperature_patch_contract_unconfirmed"
    with pytest.raises(CommandNotSupportedError):
        profile.encode_command(
            record,
            SetThermostatSetpoints(zone="PZ1", heat=68),
        )


def test_8920w_fixture_paths_are_read_without_unit_guessing() -> None:
    """Community-confirmed telemetry is explicit while its unknown unit stays unknown."""
    record = _record(
        build_thermostat_hierarchy(),
        THERMOSTAT_DEVICE_ID,
        [
            build_device_status(THERMOSTAT_DEVICE_ID, model="8920W"),
            _fixture("thermostat_8920w_settings.json"),
            _fixture("thermostat_8920w_zone_status.json"),
        ],
    )
    profile = get_profile(record.profile_key)
    assert profile is not None
    normalized = cast(NormalizedThermostatState, profile.normalize(record))
    zone = normalized.zones["PZ1"]

    assert zone.current_temperature == 21
    assert zone.current_humidity == 45
    assert zone.heat_setpoint == 20
    assert zone.cool_setpoint == 24
    assert zone.heating_status == "heating"
    assert zone.cooling_status == "idle"
    assert zone.fan_on is True
    assert zone.temperature_unit is None


def test_attached_humidifier_fixture_has_global_exact_codecs() -> None:
    """The attached humidifier contract is global and uses captured keys exactly."""
    settings = _fixture("thermostat_8920w_settings.json")
    attached = _fixture("thermostat_attached_humidifier.json")
    settings.update(attached["settings"])
    record = _record(
        build_thermostat_hierarchy(),
        THERMOSTAT_DEVICE_ID,
        [
            build_device_status(THERMOSTAT_DEVICE_ID, model="8920W"),
            settings,
            _fixture("thermostat_8920w_zone_status.json"),
            attached["status"],
        ],
    )
    profile = get_profile(record.profile_key)
    assert profile is not None
    capabilities = profile.capabilities(record)
    normalized = cast(NormalizedThermostatState, profile.normalize(record))

    assert capabilities.entities.humidifier_keys == ("attached_humidifier",)
    assert "attached_humidifier_water_panel_remaining" in (
        capabilities.entities.dynamic_sensor_keys
    )
    assert "attached_humidifier_water_panel_service" in (
        capabilities.entities.binary_sensor_keys
    )
    assert normalized.attached_humidifier is not None
    assert normalized.attached_humidifier.water_panel_remaining == 80
    assert normalized.attached_humidifier.water_panel_needs_service is False
    assert profile.encode_command(
        record,
        SetAttachedHumidifierPower(enabled=False),
    ).payload == {"humidifier": {"mode": "off"}}
    assert profile.encode_command(
        record,
        SetAttachedHumidifierTarget(humidity=42),
    ).payload == {"humidifier": {"humiditySetpoint": 42}}
    assert profile.command_confirmed(
        record,
        SetAttachedHumidifierPower(enabled=True),
    )
    assert profile.command_confirmed(
        record,
        SetAttachedHumidifierTarget(humidity=40),
    )
    assert profile.command_confirmed(
        record,
        SetThermostatMode(zone="PZ1", mode="heat"),
    )
    assert profile.command_confirmed(
        record,
        SetThermostatFan(zone="PZ1", mode="auto"),
    )
    assert profile.command_confirmed(
        record,
        SetThermostatHold(zone="PZ1", hold="permanent"),
    )
    assert not profile.command_confirmed(
        record,
        SetThermostatSetpoints(zone="PZ1", heat=20),
    )
    assert not profile.command_confirmed(
        record,
        SetThermostatMode(zone="SZ3", mode="heat"),
    )


def test_optional_attached_value_loss_does_not_remove_entities() -> None:
    """A present-but-null incremental value keeps its stable entity descriptor."""
    settings = _fixture("thermostat_8920w_settings.json")
    attached = _fixture("thermostat_attached_humidifier.json")
    settings.update(attached["settings"])
    record = _record(
        build_thermostat_hierarchy(),
        THERMOSTAT_DEVICE_ID,
        [
            build_device_status(THERMOSTAT_DEVICE_ID, model="8920W"),
            settings,
            _fixture("thermostat_8920w_zone_status.json"),
            attached["status"],
        ],
    )
    profile = get_profile(record.profile_key)
    assert profile is not None
    before = profile.entity_descriptions(record)

    record = evaluate_device_support(
        apply_device_message(
            record,
            {
                "_type": "HumidifierStatus",
                "deviceId": THERMOSTAT_DEVICE_ID,
                "asOf": "2026-03-24T00:10:00.000Z",
                "waterPanelService": {
                    "remaining": None,
                    "needsService": None,
                },
            },
        )
    )

    assert profile.entity_descriptions(record) == before
    normalized = cast(NormalizedThermostatState, profile.normalize(record))
    assert normalized.attached_humidifier is not None
    assert normalized.attached_humidifier.water_panel_remaining is None
    assert normalized.attached_humidifier.water_panel_needs_service is None


def test_ha_write_adapters_contain_no_vendor_patch_schema() -> None:
    """Home Assistant platform code creates commands, not vendor dictionaries."""
    component = Path(__file__).parents[1] / "custom_components" / "aprilaire_cloud"
    platform_source = "\n".join(
        (component / filename).read_text()
        for filename in ("climate.py", "humidifier.py", "number.py")
    )

    for vendor_key in (
        "humiditySetpoint",
        "alertLimits",
        "highHum",
        "heatSetpoint",
        "coolSetpoint",
        "ModeId",
        "FanId",
        "HoldType",
    ):
        assert vendor_key not in platform_source
