"""Device-profile registry and stable public exports."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models import DeviceRecord
from .base import (
    AprilaireCommandError,
    CommandAccessError,
    CommandCapability,
    CommandNotSupportedError,
    CommandType,
    CommandValidationError,
    DeviceCapabilities,
    DeviceCommand,
    DeviceProfile,
    EncodedCommand,
    EvidenceLevel,
    ProfileEntitySet,
    ProfileStatusRequest,
    SetAttachedHumidifierPower,
    SetAttachedHumidifierTarget,
    SetDehumidifierPower,
    SetDehumidifierTarget,
    SetHighHumidityAlert,
    SetThermostatFan,
    SetThermostatHold,
    SetThermostatMode,
    SetThermostatSetpoints,
)
from .dehumidifier import (
    DehumidifierProfile,
    NormalizedDehumidifierState,
    NormalizedTemperatureProbe,
)
from .thermostat import (
    THERMOSTAT_REPORTING_TYPE,
    THERMOSTAT_ZONE_SETTINGS_KEYS,
    NormalizedAttachedHumidifierState,
    NormalizedThermostatIAQState,
    NormalizedThermostatState,
    NormalizedThermostatZoneState,
    ThermostatProfile,
    record_has_thermostat_hint,
    thermostat_iaq_status_key_for_message,
    thermostat_status_key_for_zone,
    thermostat_status_key_from_message,
    thermostat_zone_keys_for_record,
)

UNSUPPORTED_REASON_LABELS = {
    "awaiting_device_setup": "device setup not yet available",
    "awaiting_device_settings": "device settings not yet available",
    "unsupported_equipment_type": "device family is not understood",
}
INCOMPLETE_SUPPORT_REASONS = frozenset(
    {"awaiting_device_setup", "awaiting_device_settings"}
)

DEHUMIDIFIER_PROFILE: DeviceProfile = DehumidifierProfile()
THERMOSTAT_PROFILE: DeviceProfile = ThermostatProfile()
DEVICE_PROFILES: tuple[DeviceProfile, ...] = (
    DEHUMIDIFIER_PROFILE,
    THERMOSTAT_PROFILE,
)


@dataclass(frozen=True, slots=True)
class SupportedDeviceSummary:
    """Summary of recognized and pending devices."""

    total_devices: int = 0
    supported_devices: int = 0
    unsupported_devices: int = 0
    pending_classification_devices: int = 0
    unsupported_reasons: dict[str, int] = field(default_factory=dict)


def evaluate_profile(
    record: DeviceRecord,
) -> tuple[bool, str | None, str | None, tuple[str, ...]]:
    """Return recognition, reason, profile key, and granular capability names."""
    reasons: list[str] = []
    for profile in DEVICE_PROFILES:
        reason = profile.unsupported_reason(record)
        if reason is None and profile.matches(record):
            capability_names = tuple(
                sorted(command.value for command in profile.capabilities(record).commands)
            )
            return True, None, profile.key, capability_names
        if reason is not None:
            reasons.append(reason)
    fallback_reason = next(
        (reason for reason in reasons if reason in INCOMPLETE_SUPPORT_REASONS),
        reasons[0] if reasons else None,
    )
    return False, fallback_reason, None, ()


def get_profile(profile_key: str | None) -> DeviceProfile | None:
    """Return a registered profile by key."""
    return next((profile for profile in DEVICE_PROFILES if profile.key == profile_key), None)


def capabilities_for_record(record: DeviceRecord) -> DeviceCapabilities | None:
    """Return granular capabilities for a recognized record."""
    profile = get_profile(record.profile_key)
    return profile.capabilities(record) if profile else None


def profiles_requiring_data(record: DeviceRecord) -> tuple[DeviceProfile, ...]:
    """Return a recognized profile or still-plausible pending profiles."""
    profile = get_profile(record.profile_key)
    if profile is not None:
        return (profile,)
    return tuple(
        candidate
        for candidate in DEVICE_PROFILES
        if (
            (reason := candidate.unsupported_reason(record)) is None
            or reason in INCOMPLETE_SUPPORT_REASONS
        )
    )


def status_requests_for_record(record: DeviceRecord) -> tuple[ProfileStatusRequest, ...]:
    """Return deduplicated profile endpoint requests."""
    requests: dict[tuple[str, str], ProfileStatusRequest] = {}
    for profile in profiles_requiring_data(record):
        for request in profile.status_requests(record):
            requests[(request.key, request.endpoint)] = request
    return tuple(requests.values())


def record_has_required_status(record: DeviceRecord) -> bool:
    """Return whether candidate profiles have required status."""
    profiles = profiles_requiring_data(record)
    return not profiles or all(profile.has_required_status(record) for profile in profiles)


def record_requires_rest_refresh(
    record: DeviceRecord,
    *,
    location_unhealthy: bool = False,
) -> bool:
    """Return whether a record requires REST hydration."""
    profiles = profiles_requiring_data(record)
    if (
        not record.device_setup
        and not record.device_settings
        and not record.status_payloads
    ):
        # A hierarchy reference alone has no family evidence. Fetch only the
        # critical REST status/settings pair; profile-owned optional routes are
        # planned after those responses classify the device.
        return True
    return bool(profiles) and (
        location_unhealthy
        or not record.device_settings
        or any(not profile.has_required_status(record) for profile in profiles)
    )


def get_status_payload(record: DeviceRecord, key: str) -> dict[str, Any]:
    """Return a profile status payload."""
    return record.status_payloads.get(key, {})


def normalize_device(record: DeviceRecord) -> object | None:
    """Return normalized profile state."""
    profile = get_profile(record.profile_key)
    return profile.normalize(record) if profile else None


def summarize_supported_devices(records: list[DeviceRecord]) -> SupportedDeviceSummary:
    """Summarize recognized, pending, and unsupported records."""
    reasons: dict[str, int] = {}
    supported = 0
    pending = 0
    for record in records:
        recognized, reason, _, _ = evaluate_profile(record)
        if recognized:
            supported += 1
            continue
        if reason is not None:
            reasons[reason] = reasons.get(reason, 0) + 1
            pending += reason in INCOMPLETE_SUPPORT_REASONS
    return SupportedDeviceSummary(
        total_devices=len(records),
        supported_devices=supported,
        unsupported_devices=len(records) - supported,
        pending_classification_devices=pending,
        unsupported_reasons=reasons,
    )


def format_unsupported_reasons(unsupported_reasons: dict[str, int]) -> str:
    """Return a compact human-readable reason summary."""
    parts = [
        f"{UNSUPPORTED_REASON_LABELS.get(reason, reason)} ({count})"
        for reason, count in sorted(unsupported_reasons.items())
    ]
    return ", ".join(parts) if parts else "none"


def dehumidifier_unsupported_reason(record: DeviceRecord) -> str | None:
    """Compatibility helper for dehumidifier classification tests."""
    return DEHUMIDIFIER_PROFILE.unsupported_reason(record)


__all__ = [
    "THERMOSTAT_REPORTING_TYPE",
    "THERMOSTAT_ZONE_SETTINGS_KEYS",
    "AprilaireCommandError",
    "CommandAccessError",
    "CommandCapability",
    "CommandNotSupportedError",
    "CommandType",
    "CommandValidationError",
    "DeviceCapabilities",
    "DeviceCommand",
    "DeviceProfile",
    "EncodedCommand",
    "EvidenceLevel",
    "NormalizedAttachedHumidifierState",
    "NormalizedDehumidifierState",
    "NormalizedTemperatureProbe",
    "NormalizedThermostatIAQState",
    "NormalizedThermostatState",
    "NormalizedThermostatZoneState",
    "ProfileEntitySet",
    "ProfileStatusRequest",
    "SetAttachedHumidifierPower",
    "SetAttachedHumidifierTarget",
    "SetDehumidifierPower",
    "SetDehumidifierTarget",
    "SetHighHumidityAlert",
    "SetThermostatFan",
    "SetThermostatHold",
    "SetThermostatMode",
    "SetThermostatSetpoints",
    "SupportedDeviceSummary",
    "capabilities_for_record",
    "dehumidifier_unsupported_reason",
    "evaluate_profile",
    "format_unsupported_reasons",
    "get_profile",
    "get_status_payload",
    "normalize_device",
    "record_has_required_status",
    "record_has_thermostat_hint",
    "record_requires_rest_refresh",
    "status_requests_for_record",
    "summarize_supported_devices",
    "thermostat_iaq_status_key_for_message",
    "thermostat_status_key_for_zone",
    "thermostat_status_key_from_message",
    "thermostat_zone_keys_for_record",
]
