"""Small presence, coercion, and sensor helpers shared by profiles."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

MISSING = object()


def first_present(data: dict[str, Any], *keys: str) -> Any:
    """Return the first present key, preserving false, zero, and empty values."""
    for key in keys:
        if key in data:
            return data[key]
    return MISSING


def first_nested(data: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    """Return the first fully present nested path."""
    for path in paths:
        current: Any = data
        for key in path:
            if not isinstance(current, dict) or key not in current:
                break
            current = current[key]
        else:
            return current
    return MISSING


def first_value(*values: Any) -> Any:
    """Return the first value that is not the presence sentinel."""
    return next((value for value in values if value is not MISSING), MISSING)


def coerce_float(value: Any) -> float | None:
    """Return a finite float or None."""
    if value is MISSING or value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in {float("inf"), float("-inf")} else None


def coerce_int(value: Any) -> int | None:
    """Return an integer-like value or None."""
    number = coerce_float(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def coerce_bool(value: Any) -> bool | None:
    """Return an observed boolean without truthiness guessing."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "on", "yes", "1"}:
            return True
        if normalized in {"false", "off", "no", "0"}:
            return False
    return None


def normalize_string(value: Any) -> str | None:
    """Return a normalized non-empty protocol string."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("_", "-").replace(" ", "-")
    return normalized or None


def explicit_temperature_unit(*payloads: dict[str, Any]) -> str | None:
    """Return only an explicitly reported C/F unit."""
    for payload in payloads:
        direct = first_present(payload, "temperatureUnit", "tempUnit")
        if direct is not MISSING:
            unit = str(direct).strip().upper()
            if unit in {"C", "F"}:
                return unit
        thermostat = payload.get("thermostat")
        if isinstance(thermostat, dict):
            nested = first_present(thermostat, "temperatureUnit", "tempUnit")
            if nested is not MISSING:
                unit = str(nested).strip().upper()
                if unit in {"C", "F"}:
                    return unit
    return None


def select_sensor_reading(sensors: Any) -> float | None:
    """Select controlling, reporting-primary, then first valid sensor reading."""
    if not isinstance(sensors, list):
        return None
    candidates = [
        (sensor, coerce_float(sensor.get("reading")))
        for sensor in sensors
        if isinstance(sensor, dict)
    ]
    valid = [(sensor, reading) for sensor, reading in candidates if reading is not None]
    for sensor, reading in valid:
        if sensor.get("isControlling") is True:
            return reading
    for sensor, reading in valid:
        if sensor.get("status") == "reporting" and sensor.get("isPrimary") is True:
            return reading
    return valid[0][1] if valid else None


def explicit_installation(*values: Any) -> bool | None:
    """Return explicit installed state; never infer from an empty mapping."""
    for value in values:
        if isinstance(value, bool):
            return value
        if not isinstance(value, dict):
            continue
        installed = first_present(value, "installed", "isInstalled")
        if installed is not MISSING:
            return coerce_bool(installed)
        if value:
            return True
    return None


def present_keys(data: dict[str, Any], allowed: Iterable[str]) -> tuple[str, ...]:
    """Return allowed keys present in a mapping."""
    return tuple(key for key in allowed if key in data)
