"""Edge contracts for presence-aware profile helpers."""

from __future__ import annotations

from math import inf

from custom_components.aprilaire_cloud.profiles.common import (
    MISSING,
    coerce_bool,
    coerce_float,
    coerce_int,
    explicit_installation,
    explicit_temperature_unit,
    first_nested,
    first_present,
    first_value,
    normalize_string,
    present_keys,
    select_sensor_reading,
)
from custom_components.aprilaire_cloud.profiles.thermostat import (
    thermostat_zone_from_value,
)


def test_presence_helpers_preserve_falsy_values_and_missing() -> None:
    """Falsy data is distinct from an absent path."""
    payload = {"zero": 0, "false": False, "nested": {"empty": ""}}

    assert first_present(payload, "zero", "false") == 0
    assert first_present(payload, "absent") is MISSING
    assert first_nested(payload, ("nested", "empty")) == ""
    assert first_nested(payload, ("nested", "absent")) is MISSING
    assert first_value(MISSING, False, 1) is False


def test_coercions_reject_ambiguous_or_nonfinite_values() -> None:
    """Only finite, unambiguous protocol primitives are accepted."""
    assert coerce_float(True) is None
    assert coerce_float("not-a-number") is None
    assert coerce_float(inf) is None
    assert coerce_float("1.5") == 1.5
    assert coerce_int(1.5) is None
    assert coerce_int("2") == 2
    assert coerce_bool(1) is True
    assert coerce_bool("OFF") is False
    assert coerce_bool(2) is None
    assert normalize_string(1) is None
    assert normalize_string("  ") is None
    assert normalize_string("Fan On") == "fan-on"


def test_explicit_units_and_installation_never_guess() -> None:
    """Unknown units/install state remain unknown while observed values normalize."""
    assert explicit_temperature_unit({"temperatureUnit": "K"}) is None
    assert explicit_temperature_unit({"thermostat": {"tempUnit": "c"}}) == "C"
    assert explicit_installation(False) is False
    assert explicit_installation({"installed": "yes"}) is True
    assert explicit_installation({}) is None
    assert explicit_installation("unknown") is None
    assert present_keys({"a": 0, "c": None}, ("a", "b", "c")) == ("a", "c")


def test_sensor_and_zone_selection_fallbacks_are_deterministic() -> None:
    """Reporting-primary precedes the first valid sensor without aliases."""
    sensors = [
        {"reading": "bad", "isControlling": True},
        {"reading": 41},
        {"reading": 42, "status": "reporting", "isPrimary": True},
    ]

    assert select_sensor_reading(sensors) == 42
    assert select_sensor_reading([{"reading": 41}, {"reading": 42}]) == 41
    assert select_sensor_reading("not-a-list") is None
    assert select_sensor_reading([{"reading": "bad"}]) is None
    assert thermostat_zone_from_value(1) == "PZ1"
    assert thermostat_zone_from_value("2") == "SZ2"
    assert thermostat_zone_from_value(" sz3 ") == "SZ3"
    assert thermostat_zone_from_value(True) is None
    assert thermostat_zone_from_value("unknown") is None
