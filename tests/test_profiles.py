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
    build_thermostat_setup,
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


def _thermostat_setpoint_contract(
    *,
    model: str = "8920W",
    display_unit: str = "F",
) -> DeviceRecord:
    """Return the exact captured single-zone setpoint contract."""
    setup = build_thermostat_setup()
    setup["thermostat"]["temperatureUnit"] = display_unit
    return _record(
        build_thermostat_hierarchy(),
        THERMOSTAT_DEVICE_ID,
        [
            build_device_status(THERMOSTAT_DEVICE_ID, model=model),
            setup,
            _fixture("thermostat_8920w_settings.json"),
            _fixture("thermostat_8920w_zone_status.json"),
        ],
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
    ).payload == {"thermostatPZ1": {"fan": "circ"}}
    assert profile.encode_command(
        record,
        SetThermostatFan(zone="SZ2", mode="on"),
    ).payload == {"thermostatSZ2": {"FanId": 2}}
    assert profile.encode_command(
        record,
        SetThermostatFan(zone="SZ2", mode="circulate"),
    ).payload == {"thermostatSZ2": {"FanId": 3}}
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


def test_unproven_thermostat_temperature_key_contract_stays_read_only() -> None:
    """Setpoint PATCHes remain disabled without the exact captured heat/cool keys."""
    record = _thermostat()
    profile = get_profile(record.profile_key)
    assert profile is not None
    capability = profile.capabilities(record).commands[
        CommandType.THERMOSTAT_SETPOINTS
    ]

    assert capability.writable is False
    assert capability.evidence is EvidenceLevel.CAPTURED
    assert capability.unavailable_reason == "temperature_patch_contract_unconfirmed"
    with pytest.raises(CommandNotSupportedError):
        profile.encode_command(
            record,
            SetThermostatSetpoints(zone="PZ1", heat=68),
        )


def test_8920w_fahrenheit_setpoint_contract_encodes_safe_atomic_pairs() -> None:
    """Native-C commands snap to whole F and preserve the authoritative companion."""
    record = _thermostat_setpoint_contract()
    profile = get_profile(record.profile_key)
    assert profile is not None
    capability = profile.capabilities(record).commands[
        CommandType.THERMOSTAT_SETPOINTS
    ]

    assert capability.writable is True
    assert capability.evidence is EvidenceLevel.CAPTURED
    assert capability.minimum == pytest.approx(4.444444)
    assert capability.maximum == pytest.approx(33.888889)
    assert capability.step == pytest.approx(0.555556)
    assert capability.unit == "C"

    heat_only = profile.encode_command(
        record,
        SetThermostatSetpoints(zone="PZ1", heat=(69 - 32) * 5 / 9),
    )
    assert heat_only.payload == {"thermostatPZ1": {"heat": 20.56, "cool": 24}}
    assert heat_only.command == SetThermostatSetpoints(
        zone="PZ1",
        heat=20.56,
        cool=24,
    )

    cool_only = profile.encode_command(
        record,
        SetThermostatSetpoints(zone="PZ1", cool=(72 - 32) * 5 / 9),
    )
    assert cool_only.payload == {"thermostatPZ1": {"heat": 20, "cool": 22.22}}
    assert profile.encode_command(
        record,
        SetThermostatSetpoints(
            zone="PZ1",
            heat=(69 - 32) * 5 / 9,
            cool=(72 - 32) * 5 / 9,
        ),
    ).payload == {"thermostatPZ1": {"heat": 20.56, "cool": 22.22}}


def test_8920w_setpoint_contract_validates_side_limits_and_deadband() -> None:
    """Captured F limits and the 3 F deadband reject locally without moving targets."""
    record = _thermostat_setpoint_contract()
    profile = get_profile(record.profile_key)
    assert profile is not None

    assert profile.encode_command(
        record,
        SetThermostatSetpoints(
            zone="PZ1",
            heat=(40 - 32) * 5 / 9,
            cool=(93 - 32) * 5 / 9,
        ),
    ).payload == {"thermostatPZ1": {"heat": 4.44, "cool": 33.89}}

    invalid_commands = (
        SetThermostatSetpoints(zone="PZ1", heat=(39 - 32) * 5 / 9),
        SetThermostatSetpoints(zone="PZ1", heat=(91 - 32) * 5 / 9),
        SetThermostatSetpoints(zone="PZ1", cool=(49 - 32) * 5 / 9),
        SetThermostatSetpoints(zone="PZ1", cool=(94 - 32) * 5 / 9),
        SetThermostatSetpoints(
            zone="PZ1",
            heat=(69 - 32) * 5 / 9,
            cool=(71 - 32) * 5 / 9,
        ),
        SetThermostatSetpoints(zone="PZ1", heat=(74 - 32) * 5 / 9),
    )
    for command in invalid_commands:
        with pytest.raises(CommandValidationError):
            profile.encode_command(record, command)


@pytest.mark.parametrize(
    ("model", "display_unit"),
    (
        ("unconfirmed-model", "F"),
        ("8920W", "C"),
    ),
)
def test_setpoints_stay_disabled_outside_exact_fahrenheit_contract(
    model: str,
    display_unit: str,
) -> None:
    """Unknown models and Celsius-display devices do not acquire write support."""
    record = _thermostat_setpoint_contract(model=model, display_unit=display_unit)
    profile = get_profile(record.profile_key)
    assert profile is not None

    capability = profile.capabilities(record).commands[
        CommandType.THERMOSTAT_SETPOINTS
    ]
    assert capability.writable is False
    assert capability.unavailable_reason == "temperature_patch_contract_unconfirmed"


def test_setpoints_stay_disabled_for_uncaptured_multi_zone_writes() -> None:
    """Single-zone live evidence cannot silently authorize a multi-zone layout."""
    record = _thermostat_setpoint_contract()
    settings = dict(record.device_settings)
    settings["thermostatSZ2"] = {
        "mode": "auto",
        "heat": 19,
        "cool": 23,
        "fan": "auto",
        "holdType": "permanent",
    }
    record = replace(record, device_settings=settings)
    profile = get_profile(record.profile_key)
    assert profile is not None

    capability = profile.capabilities(record).commands[
        CommandType.THERMOSTAT_SETPOINTS
    ]
    assert capability.writable is False
    with pytest.raises(CommandNotSupportedError):
        profile.encode_command(
            record,
            SetThermostatSetpoints(zone="PZ1", heat=(69 - 32) * 5 / 9),
        )


def test_setpoint_confirmation_requires_the_complete_wire_pair_with_tolerance() -> None:
    """Both authoritative values must match the encoded pair within wire precision."""
    record = _thermostat_setpoint_contract()
    profile = get_profile(record.profile_key)
    assert profile is not None
    command = SetThermostatSetpoints(zone="PZ1", heat=20.56, cool=22.22)

    confirmed = apply_device_message(
        record,
        {
            "_type": "DeviceSettings",
            "deviceId": THERMOSTAT_DEVICE_ID,
            "asOf": "2026-03-24T00:10:00.000Z",
            "thermostatPZ1": {"heat": 20.55, "cool": 22.23},
        },
    )
    rejected = apply_device_message(
        record,
        {
            "_type": "DeviceSettings",
            "deviceId": THERMOSTAT_DEVICE_ID,
            "asOf": "2026-03-24T00:10:00.000Z",
            "thermostatPZ1": {"heat": 20.54, "cool": 22.22},
        },
    )

    assert profile.command_confirmed(confirmed, command)
    assert not profile.command_confirmed(rejected, command)


def test_8920w_fixture_normalizes_native_units_staged_cooling_and_read_sensors() -> None:
    """Issue 8 telemetry remains Celsius and drives one canonical operating state."""
    record = _record(
        build_thermostat_hierarchy(),
        THERMOSTAT_DEVICE_ID,
        [
            build_device_status(THERMOSTAT_DEVICE_ID, model="8920W_GS"),
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
    assert zone.heating_status == "inactive"
    assert zone.cooling_status == "stage1"
    assert zone.fan_on is True
    assert zone.equipment_status is None
    assert zone.operating_state == "cooling"
    assert zone.temperature_unit == "C"
    assert {
        "thermostat_pz1_indoor_temperature",
        "thermostat_pz1_heat_setpoint",
        "thermostat_pz1_cool_setpoint",
        "thermostat_pz1_equipment_status",
    }.issubset(profile.capabilities(record).entities.dynamic_sensor_keys)


@pytest.mark.parametrize("model", ("8920W", "8920W_GS"))
def test_8920w_family_uses_observed_circulation_token_for_string_fan_contract(
    model: str,
) -> None:
    """Both confirmed 8920W identifiers map HA circulate to vendor circ."""
    settings = _fixture("thermostat_8920w_settings.json")
    settings["thermostatPZ1"]["fan"] = "circ"
    record = _record(
        build_thermostat_hierarchy(),
        THERMOSTAT_DEVICE_ID,
        [
            build_device_status(THERMOSTAT_DEVICE_ID, model=model),
            settings,
            _fixture("thermostat_8920w_zone_status.json"),
        ],
    )
    profile = get_profile(record.profile_key)
    assert profile is not None

    capability = profile.capabilities(record).commands[CommandType.THERMOSTAT_FAN]
    assert capability.writable is True
    assert profile.encode_command(
        record,
        SetThermostatFan(zone="PZ1", mode="circulate"),
    ).payload == {"thermostatPZ1": {"fan": "circ"}}
    assert profile.command_confirmed(
        record,
        SetThermostatFan(zone="PZ1", mode="circulate"),
    )


def test_unknown_thermostat_model_does_not_treat_display_preference_as_native_unit() -> None:
    """An unconfirmed model keeps numeric telemetry untyped instead of mislabeling it."""
    record = _record(
        build_thermostat_hierarchy(),
        THERMOSTAT_DEVICE_ID,
        [
            build_device_status(THERMOSTAT_DEVICE_ID, model="unconfirmed-model"),
            build_thermostat_setup(),
            _fixture("thermostat_8920w_settings.json"),
            _fixture("thermostat_8920w_zone_status.json"),
        ],
    )
    profile = get_profile(record.profile_key)
    assert profile is not None
    zone = cast(NormalizedThermostatState, profile.normalize(record)).zones["PZ1"]

    assert zone.current_temperature == 21
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
    assert normalized.attached_humidifier.current_humidity == 45
    assert normalized.attached_humidifier.water_panel_remaining == 80
    assert normalized.attached_humidifier.water_panel_needs_service is False
    target_capability = capabilities.commands[CommandType.ATTACHED_HUMIDIFIER_TARGET]
    assert target_capability.maximum == 50
    assert profile.encode_command(
        record,
        SetAttachedHumidifierPower(enabled=False),
    ).payload == {"humidifier": {"mode": "off"}}
    assert profile.encode_command(
        record,
        SetAttachedHumidifierTarget(humidity=42),
    ).payload == {"humidifier": {"humiditySetpoint": 42}}
    assert profile.encode_command(
        record,
        SetAttachedHumidifierTarget(humidity=50),
    ).payload == {"humidifier": {"humiditySetpoint": 50}}
    with pytest.raises(CommandValidationError):
        profile.encode_command(
            record,
            SetAttachedHumidifierTarget(humidity=51),
        )
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


def test_attached_humidifier_does_not_choose_between_multiple_zones() -> None:
    """A global humidifier remains unbound when more than one zone can report humidity."""
    record = _thermostat()
    profile = get_profile(record.profile_key)
    assert profile is not None
    normalized = cast(NormalizedThermostatState, profile.normalize(record))

    assert normalized.attached_humidifier is not None
    assert normalized.attached_humidifier.current_humidity is None


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
