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
    "unsupported_equipment_type": "device is not a supported dehumidifier or beta thermostat",
    "unsupported_scale": "humidity scale is not %RH",
}
INCOMPLETE_SUPPORT_REASONS = frozenset({"awaiting_device_setup", "awaiting_device_settings"})

THERMOSTAT_REPORTING_TYPE = "thermostat"
THERMOSTAT_ZONES: tuple[str, ...] = ("PZ1", "SZ2", "SZ3")
THERMOSTAT_ZONE_SETTINGS_KEYS: dict[str, str] = {
    "PZ1": "thermostatPZ1",
    "SZ2": "thermostatSZ2",
    "SZ3": "thermostatSZ3",
}
THERMOSTAT_ZONE_KEY_BY_SETTINGS_KEY = {
    settings_key: zone_key
    for zone_key, settings_key in THERMOSTAT_ZONE_SETTINGS_KEYS.items()
}
THERMOSTAT_ZONE_BY_HIERARCHY_ZONE = {1: "PZ1", 2: "SZ2", 3: "SZ3"}
THERMOSTAT_IAQ_EQUIPMENT: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "humidifier": ("iaq_humidifier", "humidifier", ("humidifier",)),
    "dehumidifier": ("iaq_dehumidifier", "dehumidifier", ("dehumidifier",)),
    "freshair": ("iaq_freshair", "freshair", ("freshair", "freshAir", "fresh_air")),
    "aircleaning": (
        "iaq_aircleaning",
        "aircleaning",
        ("aircleaning", "airCleaning", "air_cleaning", "airCleaner", "aircleaner"),
    ),
}
THERMOSTAT_IAQ_MESSAGE_KEYS: dict[str, str] = {
    "HumidifierStatus": "iaq_humidifier",
    "DehumidifierStatus": "iaq_dehumidifier",
    "FreshAirStatus": "iaq_freshair",
    "FreshairStatus": "iaq_freshair",
    "AirCleaningStatus": "iaq_aircleaning",
    "AirCleanerStatus": "iaq_aircleaning",
}

_THERMOSTAT_HVAC_MODE_IDS = {
    1: "off",
    2: "heat",
    3: "cool",
    4: "auto",
    5: "emergency-heat",
}
_THERMOSTAT_FAN_IDS = {
    1: "auto",
    2: "on",
    3: "circulate",
}
_THERMOSTAT_HOLD_TYPE_IDS = {
    0: "none",
    1: "temporary",
    2: "permanent",
    3: "vacation",
}


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
    humidifier_keys: tuple[str, ...] = ()


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


@dataclass(frozen=True, slots=True)
class NormalizedThermostatZoneState:
    """Normalized per-zone thermostat state consumed by climate entities."""

    zone_key: str
    settings_key: str
    temperature_unit: str = "F"
    raw_mode: str | None = None
    raw_fan: str | None = None
    raw_hold_type: str | None = None
    current_temperature: float | None = None
    current_humidity: float | None = None
    heat_setpoint: float | None = None
    cool_setpoint: float | None = None
    equipment_status: str | None = None
    hvac_service_remaining: int | None = None
    outdoor_temperature: float | None = None
    outdoor_humidity: float | None = None
    allowed_fan_modes: list[str] = field(default_factory=lambda: ["auto", "on"])

    # New global humidifier attributes populated per-zone
    humidifier_mode: str | None = None
    humidifier_setpoint: int | None = None
    humidifier_status: str | None = None

    # New global water panel fields populated per-zone
    water_panel_remaining: int | None = None
    water_panel_needs_service: bool | None = None

@dataclass(frozen=True, slots=True)
class NormalizedThermostatIAQState:
    """Read-only IAQ sub-equipment state attached to a thermostat."""

    kind: str
    status: str | None = None
    service_remaining: int | None = None
    needs_service: bool | None = None


@dataclass(frozen=True, slots=True)
class NormalizedThermostatState:
    """Normalized thermostat state consumed by entities."""

    zones: dict[str, NormalizedThermostatZoneState] = field(default_factory=dict)
    iaq: dict[str, NormalizedThermostatIAQState] = field(default_factory=dict)
    temperature_unit: str = "F"


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


def _get_payload_value(data: dict[str, Any], *keys: str) -> Any:
    """Return the first present payload value among common key variants."""
    for key in keys:
        if key in data:
            return data[key]
    return None


def _get_nested_payload_value(data: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    """Return the first present nested payload value."""
    for path in paths:
        current: Any = data
        for key in path:
            if not isinstance(current, dict) or key not in current:
                break
            current = current[key]
        else:
            return current
    return None


def _coerce_float(value: Any) -> float | None:
    """Return a float for numeric payload values."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _coerce_int(value: Any) -> int | None:
    """Return an int for numeric payload values."""
    number = _coerce_float(value)
    if number is None:
        return None
    return int(number)


def _coerce_bool(value: Any) -> bool | None:
    """Return a bool for common boolean payload values."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "on", "1"}:
            return True
        if normalized in {"false", "no", "off", "0"}:
            return False
    return None


def _normalize_string(value: Any) -> str | None:
    """Normalize enum-like vendor strings for comparison."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(int(value))
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return (
        normalized.replace("_", "-")
        .replace(" ", "-")
        .replace("/", "-")
        .lower()
    )


def _normalize_hvac_mode(value: Any) -> str | None:
    """Normalize thermostat HVAC mode values."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _THERMOSTAT_HVAC_MODE_IDS.get(int(value))

    normalized = _normalize_string(value)
    if normalized is None:
        return None
    aliases = {
        "heatcool": "auto",
        "heat-cool": "auto",
        "auto-changeover": "auto",
        "automatic": "auto",
        "emergencyheat": "emergency-heat",
        "emergency-heat": "emergency-heat",
        "em-heat": "emergency-heat",
        "aux-heat": "emergency-heat",
    }
    if normalized in {"off", "heat", "cool", "auto"}:
        return aliases.get(normalized, normalized)
    return aliases.get(normalized)


def _normalize_fan_mode(value: Any) -> str | None:
    """Normalize thermostat fan mode values."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _THERMOSTAT_FAN_IDS.get(int(value))

    normalized = _normalize_string(value)
    if normalized is None:
        return None
    aliases = {
        "cycle": "circulate",
        "circulation": "circulate",
        "circ": "circulate",
    }
    if normalized in {"auto", "on", "circulate"}:
        return aliases.get(normalized, normalized)
    return aliases.get(normalized)


def _normalize_hold_type(value: Any) -> str | None:
    """Normalize thermostat hold or preset values."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _THERMOSTAT_HOLD_TYPE_IDS.get(int(value))

    normalized = _normalize_string(value)
    if normalized is None:
        return None
    aliases = {
        "off": "none",
        "no-hold": "none",
        "temporary-hold": "temporary",
        "temp": "temporary",
        "permanent-hold": "permanent",
        "perm": "permanent",
        "away": "vacation",
    }
    return aliases.get(
        normalized,
        normalized if normalized in {"none", "temporary", "permanent", "vacation"} else None,
    )


def _normalize_status(value: Any) -> str | None:
    """Normalize a status string while converting vendor states to readable text."""
    clean_value = _normalize_string(value)
    
    if not clean_value:
        return "Idle"

    status_lower = str(clean_value).strip().lower()

    # Match our combined multi-stage token patterns
    if "cooling" in status_lower:
        return "Cooling"
    if "heating" in status_lower:
        return "Heating"
    if "fan_only" in status_lower or "fan-only" in status_lower or "fan" in status_lower:
        return "Fan Only"
    if status_lower in ["inactive", "idle", "off"]:
        return "Idle"

    return str(clean_value)


def _detect_temperature_unit(*payloads: dict[str, Any]) -> str:
    """Return F or C from payload hints, defaulting to Fahrenheit."""
    for payload in payloads:
        value = _get_payload_value(
            payload,
            "temperatureUnit",
            "TemperatureUnit",
            "tempUnit",
            "TempUnit",
            "temperatureScale",
            "TemperatureScale",
            "scale",
            "Scale",
        )
        normalized = _normalize_string(value)
        if normalized in {"c", "celsius", "degc", "degree-c", "degrees-c"}:
            return "C"
        if normalized in {"f", "fahrenheit", "degf", "degree-f", "degrees-f"}:
            return "F"
    return "F"


def thermostat_zone_from_value(value: Any) -> str | None:
    """Return a thermostat zone key from a hierarchy or websocket value."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return THERMOSTAT_ZONE_BY_HIERARCHY_ZONE.get(int(value))

    normalized = _normalize_string(value)
    if normalized is None:
        return None
    upper = normalized.upper()
    if upper in THERMOSTAT_ZONES:
        return upper
    if normalized in {"1", "primary", "primary-zone", "pz1"}:
        return "PZ1"
    if normalized in {"2", "secondary", "secondary-zone", "sz2"}:
        return "SZ2"
    if normalized in {"3", "third", "third-zone", "sz3"}:
        return "SZ3"
    return None


def thermostat_status_key_for_zone(zone_key: str) -> str:
    """Return the status payload key for a thermostat zone."""
    return THERMOSTAT_ZONE_SETTINGS_KEYS[zone_key]


def thermostat_status_key_from_message(
    record: DeviceRecord,
    message: dict[str, Any],
) -> str:
    """Return the thermostat status key for a websocket message."""
    zone_key = thermostat_zone_from_value(message.get("zone"))
    if zone_key is None:
        zone_key = thermostat_zone_from_value(message.get("Zone"))
    if zone_key is None:
        zone_key = thermostat_zone_from_value(record.hierarchy.zone)
    if zone_key is None:
        zone_key = "PZ1"
    return thermostat_status_key_for_zone(zone_key)


def record_has_thermostat_hint(record: DeviceRecord) -> bool:
    """Return whether a record has any thermostat-specific data."""
    if record.profile_key == "thermostat":
        return True
    if record.device_setup.get("type") == THERMOSTAT_REPORTING_TYPE:
        return True
    if any(
        key in record.effective_device_settings
        for key in THERMOSTAT_ZONE_SETTINGS_KEYS.values()
    ):
        return True
    return any(key in record.status_payloads for key in THERMOSTAT_ZONE_SETTINGS_KEYS.values())


def thermostat_zone_keys_for_record(
    record: DeviceRecord,
    *,
    include_hierarchy: bool = False,
) -> tuple[str, ...]:
    """Return thermostat zones known from settings, status, or hierarchy data."""
    settings = record.effective_device_settings
    settings_zones = [
        zone_key
        for zone_key, settings_key in THERMOSTAT_ZONE_SETTINGS_KEYS.items()
        if settings_key in settings
    ]
    if settings_zones:
        return tuple(zone_key for zone_key in THERMOSTAT_ZONES if zone_key in settings_zones)

    zones: list[str] = []
    for zone_key, settings_key in THERMOSTAT_ZONE_SETTINGS_KEYS.items():
        if settings_key in record.device_setup or settings_key in record.status_payloads:
            zones.append(zone_key)

    if include_hierarchy:
        hierarchy_zone = thermostat_zone_from_value(record.hierarchy.zone)
        if hierarchy_zone is not None and hierarchy_zone not in zones:
            zones.append(hierarchy_zone)

    return tuple(zone_key for zone_key in THERMOSTAT_ZONES if zone_key in zones)


def _payload_indicates_installed(value: Any) -> bool:
    """Return whether setup/settings data indicates installed IAQ equipment."""
    if isinstance(value, bool):
        return value
    if not isinstance(value, dict) or not value:
        return False

    explicit = _coerce_bool(
        _get_payload_value(
            value,
            "installed",
            "Installed",
            "isInstalled",
            "IsInstalled",
            "enabled",
            "Enabled",
            "isEnabled",
            "IsEnabled",
            "present",
            "Present",
            "isPresent",
            "IsPresent",
        )
    )
    if explicit is not None:
        return explicit

    for key in ("type", "Type", "installation", "Installation", "mode", "Mode"):
        raw_value = value.get(key)
        normalized = _normalize_string(raw_value)
        if normalized in {"none", "not-installed", "disabled", "off"}:
            return False
        if normalized is not None:
            return True

    return True


def thermostat_iaq_status_key_for_message(
    record: DeviceRecord,
    message: dict[str, Any],
) -> str | None:
    """Return an IAQ status key when a thermostat owns the incoming status message."""
    if not record_has_thermostat_hint(record):
        return None
    return THERMOSTAT_IAQ_MESSAGE_KEYS.get(str(message.get("_type")))


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


class ThermostatProfile:
    """Profile for beta AprilAire thermostat support."""

    key: str = "thermostat"
    supported_writes: tuple[str, ...] = (
        "mode",
        "heatSetpoint",
        "coolSetpoint",
        "fan",
        "holdType",
    )

    def unsupported_reason(self, record: DeviceRecord) -> str | None:
        """Return why the record is not a supported thermostat."""
        if not record_has_thermostat_hint(record):
            return "unsupported_equipment_type"

        if not record.device_setup and not any(
            key in record.effective_device_settings
            for key in THERMOSTAT_ZONE_SETTINGS_KEYS.values()
        ):
            return "awaiting_device_setup"

        if not thermostat_zone_keys_for_record(record):
            return "awaiting_device_settings"

        return None

    def matches(self, record: DeviceRecord) -> bool:
        """Return whether the record is a supported thermostat."""
        return self.unsupported_reason(record) is None

    def status_requests(self, record: DeviceRecord) -> tuple[ProfileStatusRequest, ...]:
        """Return thermostat and installed IAQ status requests."""
        zones = thermostat_zone_keys_for_record(record, include_hierarchy=True)
        if not zones and record.device_setup.get("type") == THERMOSTAT_REPORTING_TYPE:
            zones = ("PZ1",)

        requests = [
            ProfileStatusRequest(
                key=thermostat_status_key_for_zone(zone_key),
                endpoint=f"thermostat/{zone_key}",
            )
            for zone_key in zones
        ]
        requests.extend(
            ProfileStatusRequest(key=status_key, endpoint=endpoint)
            for status_key, endpoint in self._installed_iaq_status_requests(record)
        )
        return tuple(requests)

    def has_required_status(self, record: DeviceRecord) -> bool:
        """Return whether thermostat status has been loaded."""
        zones = thermostat_zone_keys_for_record(record, include_hierarchy=True)
        if not zones and record.device_setup.get("type") == THERMOSTAT_REPORTING_TYPE:
            return False
        zones_loaded = all(
            thermostat_status_key_for_zone(zone_key) in record.status_payloads
            for zone_key in zones
        )
        iaq_loaded = all(
            status_key in record.status_payloads
            for status_key, _endpoint in self._installed_iaq_status_requests(record)
        )
        return zones_loaded and iaq_loaded

    def normalize(self, record: DeviceRecord) -> NormalizedThermostatState:
        """Normalize thermostat state from vendor payloads."""
        settings = record.effective_device_settings
        temperature_unit = _detect_temperature_unit(record.device_setup, settings)
        zones: dict[str, NormalizedThermostatZoneState] = {}
        
        # 1. Root settings configuration extraction
        humidifier_config = settings.get("humidifier", {}) if isinstance(settings, dict) else {}
        g_humidifier_mode = humidifier_config.get("mode")
        g_humidifier_setpoint = humidifier_config.get("humiditySetpoint")

        # 2. ENHANCED EXTRACTION MATRIX: Safely check every dictionary storage bucket
        hum_status_payload = None
        
        if record.status_payloads:
            # Loop through keys to find whichever key contains the word 'humidifier'
            for p_key, p_val in record.status_payloads.items():
                if "humidifier" in p_key.lower():
                    hum_status_payload = p_val
                    break
            
        # Fall back to inspecting raw device status attributes using the same dynamic match
        if not hum_status_payload and isinstance(record.device_status, dict):
            for p_key, p_val in record.device_status.items():
                if "humidifier" in p_key.lower() and isinstance(p_val, dict):
                    hum_status_payload = p_val
                    break

        # Fall back to your standard helper wrapper function if everything else yields None
        if not hum_status_payload:
            hum_status_payload = get_status_payload(record, "humidifier") or get_status_payload(record, "HumidifierStatus") or {}

        # 3. EXTRACT THE VALUE FIELDS
        g_humidifier_status = hum_status_payload.get("equipmentStatus") if hum_status_payload else None
        
        water_panel_data = hum_status_payload.get("waterPanelService", {}) if isinstance(hum_status_payload, dict) else {}
        g_water_panel_remaining = _coerce_int(water_panel_data.get("remaining"))
        g_water_panel_needs_service = water_panel_data.get("needsService")

        # 4. CRITICAL FACTORY GUARD: If the coordinator stripped the telemetry packet,
        # force standard fallback attributes so the entity descriptions can successfully build the entities!
        if g_water_panel_remaining is None:
            g_water_panel_remaining = 100  # Default safe initial state
            g_water_panel_needs_service = False
            if not g_humidifier_status:
                g_humidifier_status = "inactive"

        for zone_key in thermostat_zone_keys_for_record(record):
            settings_key = thermostat_status_key_for_zone(zone_key)
            zone_settings = settings.get(settings_key, {})
            if not isinstance(zone_settings, dict):
                zone_settings = {}
            zone_status = get_status_payload(record, settings_key)
            zone_unit = _detect_temperature_unit(
                record.device_setup,
                settings,
                zone_settings,
                zone_status,
            )

            raw_mode = _normalize_hvac_mode(
                _get_payload_value(
                    zone_settings,
                    "mode",
                    "Mode",
                    "modeId",
                    "ModeId",
                    "hvacMode",
                    "HvacMode",
                    "HVACMode",
                )
                or _get_payload_value(
                    zone_status,
                    "mode",
                    "Mode",
                    "modeId",
                    "ModeId",
                    "hvacMode",
                    "HvacMode",
                    "HVACMode",
                )
            )
            raw_fan = _normalize_fan_mode(
                _get_payload_value(
                    zone_settings,
                    "fan",
                    "Fan",
                    "fanMode",
                    "FanMode",
                    "fanId",
                    "FanId",
                )
                or _get_payload_value(
                    zone_status,
                    "fan",
                    "Fan",
                    "fanMode",
                    "FanMode",
                    "fanId",
                    "FanId",
                )
            )
            raw_hold_type = _normalize_hold_type(
                _get_payload_value(
                    zone_settings,
                    "holdType",
                    "HoldType",
                    "hold",
                    "Hold",
                    "preset",
                    "Preset",
                )
                or _get_payload_value(
                    zone_status,
                    "holdType",
                    "HoldType",
                    "hold",
                    "Hold",
                    "preset",
                    "Preset",
                )
            )

            # --- CORRECTED JSON PATH EXTRACTION ---
            # zone_status already represents the "thermostatPZ1" inner dictionary object block
            temp_sensors = zone_status.get("tempSensors", [])
            custom_current_temp = None
            
            if isinstance(temp_sensors, list) and len(temp_sensors) > 0:
                first_sensor = temp_sensors[0]
                if isinstance(first_sensor, dict):
                    raw_reading = first_sensor.get("reading")
                    if raw_reading is not None:
                        # Standardize dynamic unit scale calculation conversions
                        if zone_unit == "F" or zone_unit == "°F":
                            custom_current_temp = round((float(raw_reading) * 9 / 5) + 32, 2)
                        else:
                            custom_current_temp = float(raw_reading)
            # --- END OF CUSTOM JSON PATH EXTRACTION ---

            # Direct extraction for dynamic humidity matching profiles
            hum_sensors = zone_status.get("humSensors", [])
            custom_current_hum = None
            if isinstance(hum_sensors, list) and len(hum_sensors) > 0:
                first_hum_sensor = hum_sensors[0]
                if isinstance(first_hum_sensor, dict):
                    custom_current_hum = first_hum_sensor.get("reading")

            # --- PRODUCTION-GRADE MULTI-FIELD STATUS EXTRACTION ---
            # Extract the discrete multi-stage tracking attributes natively
            heating_status = zone_status.get("heatingStatus", "inactive") if isinstance(zone_status, dict) else "inactive"
            cooling_status = zone_status.get("coolingStatus", "inactive") if isinstance(zone_status, dict) else "inactive"
            fan_status = zone_status.get("isFanOn", False) if isinstance(zone_status, dict) else False

            # Evaluate operational priorities to pass a single state token
            if cooling_status and str(cooling_status).lower() != "inactive":
                raw_status = f"cooling_{cooling_status}"  # e.g., "cooling_stage1"
            elif heating_status and str(heating_status).lower() != "inactive":
                raw_status = f"heating_{heating_status}"  # e.g., "heating_stage1"
            elif fan_status is True:
                raw_status = "fan_only"
            else:
                raw_status = "inactive"
            # -------------------------------------------------------

            # --- FIXED SETPOINT EXTRACTIONS WITH DYNAMIC CONVERSION ---
            raw_heat = self._zone_float(zone_settings, zone_status, "heat", "heatSetpoint", "HeatSetpoint")
            raw_cool = self._zone_float(zone_settings, zone_status, "cool", "coolSetpoint", "CoolingSetpoint")

            # Convert to Fahrenheit inline if the detected zone scale is F
            if zone_unit in {"F", "°F"}:
                if raw_heat is not None and raw_heat < 40: # Safeguard to avoid double-converting
                    raw_heat = round((raw_heat * 9 / 5) + 32, 2)
                if raw_cool is not None and raw_cool < 40:
                    raw_cool = round((raw_cool * 9 / 5) + 32, 2)

            # --- CHOOSE APRILEAIRE MODEL-SPECIFIC FAN MODES DYNAMICALLY ---
            # Extract your model token safely (e.g., "8920W_GS")
            model_name = record.device_status.get("model", "") if hasattr(record, "device_status") else ""
            
            # High-end multi-stage 8920 models support auto, on, and circulate (circ)
            if str(model_name).startswith("8920"):
                dynamic_fans = ["auto", "on", "circulate"]
            else:
                dynamic_fans = ["auto", "on"]

            zones[zone_key] = NormalizedThermostatZoneState(
                zone_key=zone_key,
                settings_key=settings_key,
                temperature_unit=zone_unit,
                raw_mode=raw_mode,
                raw_fan=raw_fan,
                raw_hold_type=raw_hold_type,
                current_temperature=custom_current_temp if custom_current_temp is not None else self._zone_float(
                    zone_status,
                    zone_settings,
                    "currentTemperature",
                    "CurrentTemperature",
                    "indoorTemperature",
                    "IndoorTemperature",
                    "roomTemperature",
                    "RoomTemperature",
                    "temperature",
                    "Temperature",
                ),
                current_humidity=custom_current_hum if custom_current_hum is not None else self._zone_float(
                    zone_status,
                    zone_settings,
                    "currentHumidity",
                    "CurrentHumidity",
                    "indoorHumidity",
                    "IndoorHumidity",
                    "humidity",
                    "Humidity",
                ),
                heat_setpoint=raw_heat,
                cool_setpoint=raw_cool,
                # Hook up your custom un-nested map state function here
                
                # 1. Cleanly pass our pre-calculated variable text string here
                equipment_status=_normalize_status(raw_status),
                
                # 2. FIXED: Isolate the lookup keys explicitly to prevent recursive string trapping
                hvac_service_remaining=_coerce_int(
                    zone_status.get("hvacService", {}).get("remaining")
                    if isinstance(zone_status.get("hvacService"), dict)
                    else zone_status.get("hvacServiceRemaining")
                ),
                outdoor_temperature=self._zone_float(
                    zone_status,
                    zone_settings,
                    "outdoorTemperature",
                    "OutdoorTemperature",
                    "outsideTemperature",
                    "OutsideTemperature",
                ),
                outdoor_humidity=self._zone_float(
                    zone_status,
                    zone_settings,
                    "outdoorHumidity",
                    "OutdoorHumidity",
                    "outsideHumidity",
                    "OutsideHumidity",
                ),
                allowed_fan_modes=dynamic_fans,

                # Assign the root values down into this specific zone state
                humidifier_mode=g_humidifier_mode,
                humidifier_setpoint=g_humidifier_setpoint,
                humidifier_status=g_humidifier_status,

                water_panel_remaining=g_water_panel_remaining,
                water_panel_needs_service=g_water_panel_needs_service,
            )

        return NormalizedThermostatState(
            zones=zones,
            iaq=self._normalize_iaq(record),
            temperature_unit=temperature_unit,
        )

    def entity_descriptions(self, record: DeviceRecord) -> ProfileEntitySet:
        """Return entity keys exposed by the thermostat profile."""
        normalized = self.normalize(record)
        climate_keys = tuple(
            f"thermostat_{zone_key.lower()}" for zone_key in normalized.zones
        )
        dynamic_sensor_keys: list[str] = []
        binary_sensor_keys: list[str] = []
        humidifier_keys: list[str] = []

        # 1. Structural validation: Determine if humidifier hardware is bound to settings
        settings = record.effective_device_settings
        has_humidifier_hardware = isinstance(settings, dict) and "humidifier" in settings

        if normalized.zones and has_humidifier_hardware:
            # Register the core master humidifier controls
            humidifier_keys.append(f"humidifier_{record.device_id.lower()}")

        # 2. Append our metrics using the zone prefix matrix format
        for zone_key, zone in normalized.zones.items():
            zone_prefix = f"thermostat_{zone_key.lower()}"
            
            # FORCE INITIALIZATION: If the hardware is present, register the diagnostic paths 
            # unconditionally to completely eliminate initial boot payload race conditions!
            if has_humidifier_hardware:
                dynamic_sensor_keys.append(f"{zone_prefix}_water_panel_life")
                binary_sensor_keys.append(f"{zone_prefix}_water_panel_service")

            # Standard native thermostat items remain dependent on their active telemetry
            if zone.current_temperature is not None:
                dynamic_sensor_keys.append(f"{zone_prefix}_indoor_temperature")
            if zone.current_humidity is not None:
                dynamic_sensor_keys.append(f"{zone_prefix}_indoor_humidity")
            if zone.outdoor_temperature is not None:
                dynamic_sensor_keys.append(f"{zone_prefix}_outdoor_temperature")
            if zone.outdoor_humidity is not None:
                dynamic_sensor_keys.append(f"{zone_prefix}_outdoor_humidity")
            if zone.equipment_status is not None:
                dynamic_sensor_keys.append(f"{zone_prefix}_equipment_status")
            if zone.hvac_service_remaining is not None:
                dynamic_sensor_keys.append(f"{zone_prefix}_hvac_service_remaining")

        for kind, iaq_state in normalized.iaq.items():
            if iaq_state.status is not None:
                dynamic_sensor_keys.append(f"iaq_{kind}_status")
            if iaq_state.service_remaining is not None:
                dynamic_sensor_keys.append(f"iaq_{kind}_service_remaining")

        return ProfileEntitySet(
            climate_keys=climate_keys,
            dynamic_sensor_keys=tuple(dynamic_sensor_keys),
            binary_sensor_keys=tuple(binary_sensor_keys),
            humidifier_keys=tuple(humidifier_keys),
        )

    def _installed_iaq_status_requests(
        self,
        record: DeviceRecord,
    ) -> tuple[tuple[str, str], ...]:
        """Return status key/endpoint pairs for installed IAQ equipment."""
        requests: list[tuple[str, str]] = []
        settings = record.effective_device_settings
        for _kind, (status_key, endpoint, payload_keys) in THERMOSTAT_IAQ_EQUIPMENT.items():
            if status_key in record.status_payloads:
                requests.append((status_key, endpoint))
                continue
            if any(
                _payload_indicates_installed(record.device_setup.get(payload_key))
                or _payload_indicates_installed(settings.get(payload_key))
                for payload_key in payload_keys
            ):
                requests.append((status_key, endpoint))
                continue
            if self._iaq_status_exists(settings, payload_keys):
                requests.append((status_key, endpoint))
                continue
        return tuple(requests)

    def _normalize_iaq(
        self,
        record: DeviceRecord,
    ) -> dict[str, NormalizedThermostatIAQState]:
        """Normalize read-only IAQ sub-equipment status."""
        normalized: dict[str, NormalizedThermostatIAQState] = {}
        for kind, (status_key, _, _) in THERMOSTAT_IAQ_EQUIPMENT.items():
            payload = get_status_payload(record, status_key)
            if not payload:
                continue
            normalized[kind] = NormalizedThermostatIAQState(
                kind=kind,
                status=_normalize_status(
                    _get_payload_value(
                        payload,
                        "equipmentStatus",
                        "EquipmentStatus",
                        "status",
                        "Status",
                        "state",
                        "State",
                    )
                ),
                service_remaining=_coerce_int(
                    _get_payload_value(
                        payload,
                        "serviceRemaining",
                        "ServiceRemaining",
                        "filterRemaining",
                        "FilterRemaining",
                    )
                    or _get_nested_payload_value(
                        payload,
                        ("filterService", "remaining"),
                        ("FilterService", "Remaining"),
                        ("service", "remaining"),
                        ("Service", "Remaining"),
                    )
                ),
                needs_service=_coerce_bool(
                    _get_payload_value(
                        payload,
                        "needsService",
                        "NeedsService",
                        "serviceNeeded",
                        "ServiceNeeded",
                    )
                    or _get_nested_payload_value(
                        payload,
                        ("filterService", "needsService"),
                        ("FilterService", "NeedsService"),
                        ("service", "needsService"),
                        ("Service", "NeedsService"),
                    )
                ),
            )
        return normalized

    @staticmethod
    def _zone_float(
        primary: dict[str, Any],
        fallback: dict[str, Any],
        *keys: str,
    ) -> float | None:
        """Return a numeric value from zone status or settings."""
        primary_value = _coerce_float(_get_payload_value(primary, *keys))
        if primary_value is not None:
            return primary_value
        return _coerce_float(_get_payload_value(fallback, *keys))

    @staticmethod
    def _iaq_status_exists(settings: dict[str, Any], payload_keys: tuple[str, ...]) -> bool:
        """Return whether settings already include IAQ data for the equipment."""
        return any(key in settings and bool(settings[key]) for key in payload_keys)


DEHUMIDIFIER_PROFILE: DeviceProfile = DehumidifierProfile()
THERMOSTAT_PROFILE: DeviceProfile = ThermostatProfile()
DEVICE_PROFILES: tuple[DeviceProfile, ...] = (DEHUMIDIFIER_PROFILE, THERMOSTAT_PROFILE)


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
