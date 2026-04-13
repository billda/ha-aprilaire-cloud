"""Device profiles and normalized device state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .const import (
    DEHUMIDIFIER_CONTROL_TYPE,
    DEHUMIDIFIER_REPORTING_TYPE,
    DEHUMIDIFIER_SCALE,
)
from .models import DeviceRecord

UNSUPPORTED_REASON_LABELS: dict[str, str] = {
    "awaiting_device_setup": "device setup not yet available",
    "awaiting_device_settings": "device settings not yet available",
    "dryness_setpoint_unsupported": "dryness setpoint devices are unsupported",
    "missing_humidity_setpoint": "humidity setpoint is missing",
    "unsupported_control_type": "control type is not internal",
    "unsupported_equipment_type": "device is not a supported dehumidifier",
    "unsupported_scale": "humidity scale is not %RH",
}
INCOMPLETE_SUPPORT_REASONS = frozenset({"awaiting_device_setup", "awaiting_device_settings"})


@dataclass(frozen=True, slots=True)
class SupportedDeviceSummary:
    """Summary of supported and unsupported devices."""

    total_devices: int = 0
    supported_devices: int = 0
    unsupported_devices: int = 0
    pending_classification_devices: int = 0
    unsupported_reasons: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProfileEntitySet:
    """Entity keys exposed by a profile."""

    climate_keys: tuple[str, ...] = ()
    sensor_keys: tuple[str, ...] = ()
    dynamic_sensor_keys: tuple[str, ...] = ()
    binary_sensor_keys: tuple[str, ...] = ()
    number_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProfileStatusRequest:
    """A profile-owned REST status endpoint."""

    key: str
    endpoint: str


@dataclass(frozen=True, slots=True)
class NormalizedTemperatureProbe:
    """A non-controlling temperature probe."""

    uid: int
    name: str
    reading: float | None


@dataclass(frozen=True, slots=True)
class NormalizedDehumidifierState:
    """Normalized dehumidifier state consumed by entities."""

    mode: str | None = None
    current_humidity: float | None = None
    target_humidity: int | None = None
    current_temperature: float | None = None
    equipment_status: str | None = None
    filter_remaining: int | None = None
    filter_needs_service: bool | None = None
    fan_runtime_hours: int | None = None
    wifi_rssi: int | None = None
    alert_high_humidity: bool | None = None
    alert_low_humidity: bool | None = None
    alert_high_temperature: bool | None = None
    alert_low_temperature: bool | None = None
    compressor_on: bool | None = None
    dehumidifier_fan_on: bool | None = None
    hvac_fan_on: bool | None = None
    alert_limits: dict[str, int] = field(default_factory=dict)
    extra_temperature_probes: tuple[NormalizedTemperatureProbe, ...] = ()


class DeviceProfile(Protocol):
    """Small interface for AprilAire device families."""

    key: str
    supported_writes: tuple[str, ...]

    def unsupported_reason(self, record: DeviceRecord) -> str | None:
        """Return why this profile does not currently support a record."""
        ...

    def matches(self, record: DeviceRecord) -> bool:
        """Return whether the profile supports a record."""
        ...

    def status_requests(self, record: DeviceRecord) -> tuple[ProfileStatusRequest, ...]:
        """Return REST status requests needed by this profile."""
        ...

    def has_required_status(self, record: DeviceRecord) -> bool:
        """Return whether this profile has the status payloads it needs."""
        ...

    def normalize(self, record: DeviceRecord) -> object | None:
        """Return normalized state for a record."""
        ...

    def entity_descriptions(self, record: DeviceRecord) -> ProfileEntitySet:
        """Return entity keys for a record."""
        ...


class DehumidifierProfile:
    """Profile for supported AprilAire dehumidifiers."""

    key: str = "dehumidifier"
    supported_writes: tuple[str, ...] = ("mode", "humiditySetpoint", "alertLimits.highHum")
    _status_requests: tuple[ProfileStatusRequest, ...] = (
        ProfileStatusRequest(key="dehumidifier", endpoint="dehumidifier"),
    )

    def unsupported_reason(self, record: DeviceRecord) -> str | None:
        """Return why the record is not a supported dehumidifier."""
        return dehumidifier_unsupported_reason(record)

    def matches(self, record: DeviceRecord) -> bool:
        """Return whether the record is a supported dehumidifier."""
        return self.unsupported_reason(record) is None

    def status_requests(self, record: DeviceRecord) -> tuple[ProfileStatusRequest, ...]:
        """Return dehumidifier status requests."""
        return self._status_requests

    def has_required_status(self, record: DeviceRecord) -> bool:
        """Return whether dehumidifier status has been loaded."""
        return bool(get_status_payload(record, self.key))

    def normalize(self, record: DeviceRecord) -> NormalizedDehumidifierState:
        """Normalize dehumidifier state from vendor payloads."""
        settings = record.effective_device_settings.get("dehumidifier", {})
        status = get_status_payload(record, self.key)
        alerts = status.get("alerts", {})

        current_humidity = next(
            (
                sensor.get("reading")
                for sensor in status.get("humSensors", [])
                if sensor.get("isControlling")
            ),
            None,
        )
        current_temperature = next(
            (
                sensor.get("reading")
                for sensor in status.get("tempSensors", [])
                if sensor.get("isControlling")
            ),
            None,
        )

        sensor_names = {
            sensor.get("uid"): sensor.get("dispName")
            for sensor in settings.get("sensors", [])
            if sensor.get("uid") is not None
        }
        extra_temperature_probes: list[NormalizedTemperatureProbe] = []
        for sensor in status.get("tempSensors", []):
            if sensor.get("isControlling") or sensor.get("uid") is None:
                continue
            uid = int(sensor["uid"])
            extra_temperature_probes.append(
                NormalizedTemperatureProbe(
                    uid=uid,
                    name=sensor_names.get(uid, f"Temperature {uid}") or f"Temperature {uid}",
                    reading=sensor.get("reading"),
                )
            )

        alert_limits = {
            key: int(value)
            for key, value in settings.get("alertLimits", {}).items()
            if isinstance(value, (int, float))
        }

        return NormalizedDehumidifierState(
            mode=settings.get("mode"),
            current_humidity=current_humidity,
            target_humidity=settings.get("humiditySetpoint"),
            current_temperature=current_temperature,
            equipment_status=status.get("equipmentStatus"),
            filter_remaining=status.get("filterService", {}).get("remaining"),
            filter_needs_service=status.get("filterService", {}).get("needsService"),
            fan_runtime_hours=status.get("fanTimeHours"),
            wifi_rssi=status.get("wifiRSSI"),
            alert_high_humidity=alerts.get("highHum"),
            alert_low_humidity=alerts.get("lowHum"),
            alert_high_temperature=alerts.get("highTemp"),
            alert_low_temperature=alerts.get("lowTemp"),
            compressor_on=status.get("isCompOn"),
            dehumidifier_fan_on=status.get("isDehumFanOn"),
            hvac_fan_on=status.get("isHvacFanOn"),
            alert_limits=alert_limits,
            extra_temperature_probes=tuple(extra_temperature_probes),
        )

    def entity_descriptions(self, record: DeviceRecord) -> ProfileEntitySet:
        """Return entity keys exposed by the dehumidifier profile."""
        normalized = self.normalize(record)

        sensor_keys = ["current_humidity", "current_temperature"]
        if normalized.filter_remaining is not None:
            sensor_keys.append("filter_life")
        if normalized.fan_runtime_hours is not None:
            sensor_keys.append("fan_runtime")
        if normalized.wifi_rssi is not None:
            sensor_keys.append("wifi_signal")
        if normalized.equipment_status is not None:
            sensor_keys.append("equipment_status")

        binary_sensor_keys: list[str] = []
        if normalized.filter_needs_service is not None:
            binary_sensor_keys.append("filter_service")
        if normalized.alert_high_humidity is not None:
            binary_sensor_keys.append("alert_high_humidity")
        if normalized.alert_low_humidity is not None:
            binary_sensor_keys.append("alert_low_humidity")
        if normalized.alert_high_temperature is not None:
            binary_sensor_keys.append("alert_high_temperature")
        if normalized.alert_low_temperature is not None:
            binary_sensor_keys.append("alert_low_temperature")
        if normalized.compressor_on is not None:
            binary_sensor_keys.append("compressor")
        if normalized.dehumidifier_fan_on is not None:
            binary_sensor_keys.append("dehumidifier_fan")
        if normalized.hvac_fan_on is not None:
            binary_sensor_keys.append("hvac_fan")

        number_keys = tuple(sorted(key for key in normalized.alert_limits if key in {"highHum"}))
        dynamic_sensor_keys = tuple(
            f"temperature_{uid}"
            for uid in sorted(probe.uid for probe in normalized.extra_temperature_probes)
        )

        return ProfileEntitySet(
            sensor_keys=tuple(sensor_keys),
            dynamic_sensor_keys=dynamic_sensor_keys,
            binary_sensor_keys=tuple(binary_sensor_keys),
            number_keys=number_keys,
        )


DEHUMIDIFIER_PROFILE: DeviceProfile = DehumidifierProfile()
DEVICE_PROFILES: tuple[DeviceProfile, ...] = (DEHUMIDIFIER_PROFILE,)


def dehumidifier_unsupported_reason(record: DeviceRecord) -> str | None:
    """Return the unsupported reason for the dehumidifier profile."""
    if not record.device_setup:
        return "awaiting_device_setup"
    if record.device_setup.get("type") != DEHUMIDIFIER_REPORTING_TYPE:
        return "unsupported_equipment_type"

    dehumidifier_setup = record.device_setup.get("dehumidifier", {})
    dehumidifier_settings = record.device_settings.get("dehumidifier", {})

    if not dehumidifier_setup:
        return "awaiting_device_setup"
    if not dehumidifier_settings:
        return "awaiting_device_settings"
    if dehumidifier_setup.get("controlType") != DEHUMIDIFIER_CONTROL_TYPE:
        return "unsupported_control_type"
    if dehumidifier_setup.get("scale") != DEHUMIDIFIER_SCALE:
        return "unsupported_scale"
    if "humiditySetpoint" not in dehumidifier_settings:
        return "missing_humidity_setpoint"
    if "drynessSetpoint" in dehumidifier_settings:
        return "dryness_setpoint_unsupported"
    return None


def evaluate_profile(record: DeviceRecord) -> tuple[bool, str | None, str | None, tuple[str, ...]]:
    """Return support, reason, profile key, and supported writes."""
    reasons: list[str] = []
    for profile in DEVICE_PROFILES:
        reason = profile.unsupported_reason(record)
        if reason is None and profile.matches(record):
            return True, None, profile.key, profile.supported_writes
        if reason is not None:
            reasons.append(reason)

    fallback_reason = next(
        (reason for reason in reasons if reason in INCOMPLETE_SUPPORT_REASONS),
        reasons[0] if reasons else None,
    )
    return False, fallback_reason, None, ()


def get_profile(profile_key: str | None) -> DeviceProfile | None:
    """Return a profile by key."""
    for profile in DEVICE_PROFILES:
        if profile_key == profile.key:
            return profile
    return None


def profiles_requiring_data(record: DeviceRecord) -> tuple[DeviceProfile, ...]:
    """Return profiles that are supported or still pending classification."""
    profile = get_profile(record.profile_key)
    if profile is not None:
        return (profile,)
    return tuple(
        profile
        for profile in DEVICE_PROFILES
        if (
            (reason := profile.unsupported_reason(record)) is None
            or reason in INCOMPLETE_SUPPORT_REASONS
        )
    )


def status_requests_for_record(record: DeviceRecord) -> tuple[ProfileStatusRequest, ...]:
    """Return deduplicated status requests needed to refresh a record."""
    requests: dict[tuple[str, str], ProfileStatusRequest] = {}
    for profile in profiles_requiring_data(record):
        for request in profile.status_requests(record):
            requests[(request.key, request.endpoint)] = request
    return tuple(requests.values())


def record_has_required_status(record: DeviceRecord) -> bool:
    """Return whether candidate profiles have their required status payloads."""
    profiles = profiles_requiring_data(record)
    if not profiles:
        return True
    return all(profile.has_required_status(record) for profile in profiles)


def record_requires_rest_refresh(
    record: DeviceRecord,
    *,
    location_unhealthy: bool = False,
) -> bool:
    """Return whether a record still needs profile-owned REST refresh data."""
    profiles = profiles_requiring_data(record)
    if not profiles:
        return False
    return (
        location_unhealthy
        or not record.device_settings
        or any(not profile.has_required_status(record) for profile in profiles)
    )


def get_status_payload(record: DeviceRecord, key: str) -> dict[str, Any]:
    """Return a profile-owned status payload."""
    if key in record.status_payloads:
        return record.status_payloads[key]
    return {}


def normalize_device(record: DeviceRecord) -> object | None:
    """Return normalized state for a supported device."""
    profile = get_profile(record.profile_key)
    if profile is None:
        return None
    return profile.normalize(record)


def summarize_supported_devices(records: list[DeviceRecord]) -> SupportedDeviceSummary:
    """Return supported and unsupported device counts."""
    unsupported_reasons: dict[str, int] = {}
    supported_devices = 0
    unsupported_devices = 0
    pending_classification_devices = 0
    for record in records:
        supported, reason, _, _ = evaluate_profile(record)
        if supported:
            supported_devices += 1
            continue
        unsupported_devices += 1
        if reason is not None:
            unsupported_reasons[reason] = unsupported_reasons.get(reason, 0) + 1
            if reason in INCOMPLETE_SUPPORT_REASONS:
                pending_classification_devices += 1

    return SupportedDeviceSummary(
        total_devices=len(records),
        supported_devices=supported_devices,
        unsupported_devices=unsupported_devices,
        pending_classification_devices=pending_classification_devices,
        unsupported_reasons=unsupported_reasons,
    )


def format_unsupported_reasons(unsupported_reasons: dict[str, int]) -> str:
    """Return a compact human-readable reason summary."""
    parts = [
        f"{UNSUPPORTED_REASON_LABELS.get(reason, reason)} ({count})"
        for reason, count in sorted(unsupported_reasons.items())
    ]
    return ", ".join(parts) if parts else "none"
