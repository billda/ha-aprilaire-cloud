"""AprilAire thermostat and attached IAQ profile."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from ..models import DeviceRecord
from .base import (
    CommandAccessError,
    CommandCapability,
    CommandNotSupportedError,
    CommandType,
    CommandValidationError,
    DeviceCapabilities,
    DeviceCommand,
    EncodedCommand,
    EvidenceLevel,
    ProfileEntitySet,
    ProfileStatusRequest,
    SetAttachedHumidifierPower,
    SetAttachedHumidifierTarget,
    SetThermostatFan,
    SetThermostatHold,
    SetThermostatMode,
    SetThermostatSetpoints,
)
from .common import (
    coerce_bool,
    coerce_float,
    coerce_int,
    explicit_installation,
    explicit_temperature_unit,
    first_nested,
    first_present,
    first_value,
    normalize_string,
    select_sensor_reading,
)

THERMOSTAT_REPORTING_TYPE = "thermostat"
THERMOSTAT_ZONES = ("PZ1", "SZ2", "SZ3")
THERMOSTAT_ZONE_SETTINGS_KEYS = {
    "PZ1": "thermostatPZ1",
    "SZ2": "thermostatSZ2",
    "SZ3": "thermostatSZ3",
}
THERMOSTAT_ZONE_BY_HIERARCHY_ZONE = {1: "PZ1", 2: "SZ2", 3: "SZ3"}
THERMOSTAT_IAQ_EQUIPMENT: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "humidifier": ("iaq_humidifier", "humidifier", ("humidifier",)),
    "dehumidifier": ("iaq_dehumidifier", "dehumidifier", ("dehumidifier",)),
    "freshair": ("iaq_freshair", "freshair", ("freshAir", "freshair")),
    "aircleaning": ("iaq_aircleaning", "aircleaning", ("airCleaning", "aircleaning")),
}
THERMOSTAT_IAQ_MESSAGE_KEYS = {
    "HumidifierStatus": "iaq_humidifier",
    "DehumidifierStatus": "iaq_dehumidifier",
    "FreshAirStatus": "iaq_freshair",
    "AirCleaningStatus": "iaq_aircleaning",
}

_MODE_BY_ID = {1: "off", 2: "heat", 3: "cool", 4: "auto", 5: "emergency-heat"}
_MODE_ID_BY_VALUE = {value: key for key, value in _MODE_BY_ID.items()}
_FAN_BY_ID = {1: "auto", 2: "on", 3: "circulate"}
_FAN_ID_BY_VALUE = {value: key for key, value in _FAN_BY_ID.items()}
_HOLD_BY_ID = {0: "none", 1: "temporary", 2: "permanent", 3: "vacation"}
_HOLD_ID_BY_VALUE = {value: key for key, value in _HOLD_BY_ID.items()}
_CONFIRMED_8920W_MODES = ("off", "heat", "cool", "auto")
_CONFIRMED_8920W_FANS = ("auto", "on", "circulate")
_CONFIRMED_8920W_HOLDS = ("none", "temporary", "permanent", "vacation")
_CONFIRMED_8920W_MODELS = frozenset({"8920W", "8920W_GS"})
_CONFIRMED_8920W_NATIVE_TEMPERATURE_UNIT = "C"
_ACTIVE_HEATING_STATUSES = frozenset({"active", "heating", "on", "stage1"})
_ACTIVE_COOLING_STATUSES = frozenset({"active", "cooling", "on", "stage1"})
_INACTIVE_OPERATING_STATUSES = frozenset({"idle", "inactive", "off"})
_OPERATING_STATE_BY_EQUIPMENT_STATUS = {
    "off": "off",
    "heating": "heating",
    "heat": "heating",
    "aux-heat": "heating",
    "emergency-heat": "heating",
    "cooling": "cooling",
    "cool": "cooling",
    "fan": "fan",
    "fan-only": "fan",
    "fan-on": "fan",
    "idle": "idle",
    "inactive": "idle",
    "standby": "idle",
}


@dataclass(frozen=True, slots=True)
class _EnumSettingCodec:
    """Describe one observed string/numeric thermostat enum contract."""

    string_key: str
    numeric_key: str
    numeric_values: dict[str, int]
    label: str
    string_values: dict[str, str] = field(default_factory=dict)


_MODE_CODEC = _EnumSettingCodec("mode", "ModeId", _MODE_ID_BY_VALUE, "thermostat mode")
_FAN_CODEC = _EnumSettingCodec("fan", "FanId", _FAN_ID_BY_VALUE, "fan mode")
_FAN_GS_CODEC = _EnumSettingCodec(
    "fan",
    "FanId",
    _FAN_ID_BY_VALUE,
    "fan mode",
    {"circulate": "circ"},
)
_HOLD_CODEC = _EnumSettingCodec("holdType", "HoldType", _HOLD_ID_BY_VALUE, "hold mode")


@dataclass(frozen=True, slots=True)
class NormalizedThermostatZoneState:
    """Normalized per-zone thermostat state."""

    zone_key: str
    settings_key: str
    temperature_unit: str | None = None
    raw_mode: str | None = None
    raw_fan: str | None = None
    raw_hold_type: str | None = None
    current_temperature: float | None = None
    current_humidity: float | None = None
    heat_setpoint: float | None = None
    cool_setpoint: float | None = None
    equipment_status: str | None = None
    heating_status: str | None = None
    cooling_status: str | None = None
    fan_on: bool | None = None
    operating_state: str | None = None
    hvac_service_remaining: int | None = None
    outdoor_temperature: float | None = None
    outdoor_humidity: float | None = None


@dataclass(frozen=True, slots=True)
class NormalizedThermostatIAQState:
    """Normalized attached IAQ status."""

    kind: str
    status: str | None = None
    service_remaining: int | None = None
    needs_service: bool | None = None


@dataclass(frozen=True, slots=True)
class NormalizedAttachedHumidifierState:
    """Global humidifier attached to a thermostat."""

    installed: bool
    mode: str | None = None
    current_humidity: float | None = None
    target_humidity: int | None = None
    equipment_status: str | None = None
    water_panel_remaining: int | None = None
    water_panel_needs_service: bool | None = None


@dataclass(frozen=True, slots=True)
class NormalizedThermostatState:
    """Normalized thermostat state consumed by entities."""

    zones: dict[str, NormalizedThermostatZoneState] = field(default_factory=dict)
    iaq: dict[str, NormalizedThermostatIAQState] = field(default_factory=dict)
    attached_humidifier: NormalizedAttachedHumidifierState | None = None
    temperature_unit: str | None = None


def _zone_dynamic_sensor_keys(
    record: DeviceRecord,
    zone: str,
) -> list[str]:
    """Return dynamic telemetry keys reported for one thermostat zone."""
    prefix = f"thermostat_{zone.lower()}"
    _, settings, status = _zone_mapping(record, zone)
    keys: list[str] = []
    if "tempSensors" in status or "currentTemperature" in status:
        keys.append(f"{prefix}_indoor_temperature")
    if "humSensors" in status or "currentHumidity" in status:
        keys.append(f"{prefix}_indoor_humidity")
    if any(key in settings for key in ("heatSetpoint", "HeatSetpoint", "heat")):
        keys.append(f"{prefix}_heat_setpoint")
    if any(key in settings for key in ("coolSetpoint", "CoolSetpoint", "cool")):
        keys.append(f"{prefix}_cool_setpoint")
    if "outdoorTemperature" in status:
        keys.append(f"{prefix}_outdoor_temperature")
    if "outdoorHumidity" in status:
        keys.append(f"{prefix}_outdoor_humidity")
    if any(
        key in status
        for key in ("equipmentStatus", "heatingStatus", "coolingStatus", "isFanOn")
    ):
        keys.append(f"{prefix}_equipment_status")
    if "hvacServiceRemaining" in status or "hvacService" in status:
        keys.append(f"{prefix}_hvac_service_remaining")
    return keys


def _iaq_dynamic_sensor_keys(
    record: DeviceRecord,
    normalized: NormalizedThermostatState,
) -> list[str]:
    """Return status/service keys for explicitly normalized attached IAQ."""
    keys: list[str] = []
    for kind in normalized.iaq:
        status_key = THERMOSTAT_IAQ_EQUIPMENT[kind][0]
        payload = record.status_payloads.get(status_key, {})
        if "equipmentStatus" in payload:
            keys.append(f"iaq_{kind}_status")
        if "filterService" in payload or "serviceRemaining" in payload:
            keys.append(f"iaq_{kind}_service_remaining")
    return keys


def _attached_humidifier_entity_keys(
    record: DeviceRecord,
    attached: NormalizedAttachedHumidifierState | None,
) -> tuple[list[str], list[str]]:
    """Return reported water-panel sensor and binary-sensor keys."""
    if attached is None:
        return [], []
    status = record.status_payloads.get("iaq_humidifier", {})
    panel = status.get("waterPanelService")
    panel = panel if isinstance(panel, dict) else {}
    sensors = (
        ["attached_humidifier_water_panel_remaining"]
        if "remaining" in panel
        else []
    )
    binary = (
        ["attached_humidifier_water_panel_service"]
        if "needsService" in panel
        else []
    )
    return sensors, binary


def thermostat_zone_from_value(value: Any) -> str | None:
    """Normalize only observed zone representations."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return THERMOSTAT_ZONE_BY_HIERARCHY_ZONE.get(value)
    if isinstance(value, str):
        normalized = value.strip().upper()
        if normalized in THERMOSTAT_ZONES:
            return normalized
        if normalized.isdigit():
            return THERMOSTAT_ZONE_BY_HIERARCHY_ZONE.get(int(normalized))
    return None


def thermostat_status_key_for_zone(zone_key: str) -> str:
    """Return the settings/status key for a known zone."""
    return THERMOSTAT_ZONE_SETTINGS_KEYS[zone_key]


def thermostat_status_key_from_message(
    record: DeviceRecord,
    message: dict[str, Any],
) -> str:
    """Route a status frame without assuming a primary zone."""
    zone = thermostat_zone_from_value(
        first_value(
            first_present(message, "zone"),
            first_present(message, "Zone"),
            record.hierarchy.zone,
        )
    )
    return thermostat_status_key_for_zone(zone) if zone else "thermostat_unknown"


def record_has_thermostat_hint(record: DeviceRecord) -> bool:
    """Return whether explicit payload structure identifies a thermostat."""
    return (
        record.profile_key == "thermostat"
        or record.device_setup.get("type") == THERMOSTAT_REPORTING_TYPE
        or any(
            key in record.effective_device_settings
            for key in THERMOSTAT_ZONE_SETTINGS_KEYS.values()
        )
        or any(key in record.status_payloads for key in THERMOSTAT_ZONE_SETTINGS_KEYS.values())
    )


def thermostat_zone_keys_for_record(
    record: DeviceRecord,
    *,
    include_hierarchy: bool = False,
) -> tuple[str, ...]:
    """Return zones explicitly present in settings, status, setup, or hierarchy."""
    settings_keys = {
        zone
        for zone, settings_key in THERMOSTAT_ZONE_SETTINGS_KEYS.items()
        if settings_key in record.effective_device_settings
    }
    # DeviceSettings is the authoritative capability snapshot. WebSocket
    # settings are merged, while a full REST document can explicitly remove a
    # zone without stale status retaining its entity.
    keys: set[str] = settings_keys or {
        zone
        for zone, settings_key in THERMOSTAT_ZONE_SETTINGS_KEYS.items()
        if settings_key in record.status_payloads or settings_key in record.device_setup
    }
    if include_hierarchy:
        hierarchy_zone = thermostat_zone_from_value(record.hierarchy.zone)
        if hierarchy_zone:
            keys.add(hierarchy_zone)
    return tuple(zone for zone in THERMOSTAT_ZONES if zone in keys)


def thermostat_iaq_status_key_for_message(
    record: DeviceRecord,
    message: dict[str, Any],
) -> str | None:
    """Route IAQ status only when explicit thermostat evidence exists."""
    if not record_has_thermostat_hint(record):
        return None
    return THERMOSTAT_IAQ_MESSAGE_KEYS.get(str(message.get("_type")))


def _normalize_enum(value: Any, numeric: dict[int, str]) -> str | None:
    """Normalize an observed string or released numeric enum."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return numeric.get(value)
    return normalize_string(value)


def _normalize_fan(value: Any) -> str | None:
    """Normalize the live-confirmed 8920W circulation shorthand."""
    normalized = _normalize_enum(value, _FAN_BY_ID)
    return "circulate" if normalized == "circ" else normalized


def _zone_mapping(record: DeviceRecord, zone: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Return key, settings, and status for an explicit zone."""
    settings_key = thermostat_status_key_for_zone(zone)
    settings = record.effective_device_settings.get(settings_key)
    status = record.status_payloads.get(settings_key)
    return (
        settings_key,
        settings if isinstance(settings, dict) else {},
        status if isinstance(status, dict) else {},
    )


def _numeric_from(primary: dict[str, Any], fallback: dict[str, Any], *keys: str) -> float | None:
    """Return a numeric first-present value from two payloads."""
    return coerce_float(
        first_value(first_present(primary, *keys), first_present(fallback, *keys))
    )


def _normalized_status(value: Any) -> str | None:
    """Normalize a status string while preserving unknown values."""
    return normalize_string(value)


def _thermostat_model(record: DeviceRecord) -> str | None:
    """Return an exact protocol model identifier when present."""
    model = record.device_status.get("model")
    return model if isinstance(model, str) else None


def _native_temperature_unit(
    record: DeviceRecord,
    settings: dict[str, Any],
    status: dict[str, Any],
) -> str | None:
    """Return a native payload unit, never a display-preference fallback."""
    if _thermostat_model(record) in _CONFIRMED_8920W_MODELS:
        return _CONFIRMED_8920W_NATIVE_TEMPERATURE_UNIT
    return explicit_temperature_unit(settings, status)


def _derive_operating_state(
    *,
    raw_mode: str | None,
    equipment_status: str | None,
    heating_status: str | None,
    cooling_status: str | None,
    fan_on: bool | None,
) -> str | None:
    """Derive one conservative state from observed aggregate and split fields."""
    state: str | None
    if raw_mode == "off":
        state = "off"
    elif heating_status in _ACTIVE_HEATING_STATUSES:
        state = "heating"
    elif cooling_status in _ACTIVE_COOLING_STATUSES:
        state = "cooling"
    elif fan_on is True:
        state = "fan"
    else:
        statuses = tuple(
            status for status in (heating_status, cooling_status) if status is not None
        )
        if (
            statuses
            and all(status in _INACTIVE_OPERATING_STATUSES for status in statuses)
            and fan_on is False
        ):
            state = "idle"
        elif equipment_status is not None:
            state = _OPERATING_STATE_BY_EQUIPMENT_STATUS.get(equipment_status)
        else:
            state = None
    return state


def _explicit_equipment_installation(
    record: DeviceRecord,
    payload_keys: tuple[str, ...],
    status_key: str,
) -> bool | None:
    """Return explicit IAQ installation state from setup/settings/status."""
    values = [
        record.device_setup.get(key)
        for key in payload_keys
        if key in record.device_setup
    ]
    values.extend(
        record.effective_device_settings.get(key)
        for key in payload_keys
        if key in record.effective_device_settings
    )
    installation = explicit_installation(*values)
    if installation is not None:
        return installation
    return True if status_key in record.status_payloads else None


class ThermostatProfile:
    """Recognition, normalization, capabilities, and commands for thermostats."""

    key = "thermostat"

    def unsupported_reason(self, record: DeviceRecord) -> str | None:
        """Return why a record cannot yet be represented as a thermostat."""
        if not record_has_thermostat_hint(record):
            return "unsupported_equipment_type"
        if not thermostat_zone_keys_for_record(record):
            return "awaiting_device_settings"
        return None

    def matches(self, record: DeviceRecord) -> bool:
        """Return whether the thermostat has at least one explicit zone."""
        return self.unsupported_reason(record) is None

    def status_requests(self, record: DeviceRecord) -> tuple[ProfileStatusRequest, ...]:
        """Plan explicit zone and installed IAQ endpoints."""
        requests = [
            ProfileStatusRequest(
                thermostat_status_key_for_zone(zone),
                f"thermostat/{zone}",
            )
            for zone in thermostat_zone_keys_for_record(record, include_hierarchy=True)
        ]
        requests.extend(
            ProfileStatusRequest(status_key, endpoint)
            for status_key, endpoint in self._installed_iaq_status_requests(record)
        )
        return tuple(requests)

    def has_required_status(self, record: DeviceRecord) -> bool:
        """Return whether all explicit zones and installed equipment have status."""
        zones = thermostat_zone_keys_for_record(record, include_hierarchy=True)
        return bool(zones) and all(
            thermostat_status_key_for_zone(zone) in record.status_payloads for zone in zones
        ) and all(
            status_key in record.status_payloads
            for status_key, _ in self._installed_iaq_status_requests(record)
        )

    def normalize(self, record: DeviceRecord) -> NormalizedThermostatState:
        """Normalize released and community-confirmed thermostat fields."""
        zones = {
            zone: self._normalize_zone(record, zone)
            for zone in thermostat_zone_keys_for_record(record)
        }
        native_units = {
            zone.temperature_unit for zone in zones.values() if zone.temperature_unit is not None
        }
        return NormalizedThermostatState(
            zones=zones,
            iaq=self._normalize_iaq(record),
            attached_humidifier=self._normalize_attached_humidifier(record, zones),
            temperature_unit=(native_units.pop() if len(native_units) == 1 else None),
        )

    def _normalize_zone(
        self,
        record: DeviceRecord,
        zone: str,
    ) -> NormalizedThermostatZoneState:
        """Normalize one explicit thermostat zone."""
        settings_key, settings, status = _zone_mapping(record, zone)
        current_temperature = select_sensor_reading(status.get("tempSensors"))
        if current_temperature is None:
            current_temperature = _numeric_from(
                status, settings, "currentTemperature", "CurrentTemperature"
            )
        current_humidity = select_sensor_reading(status.get("humSensors"))
        if current_humidity is None:
            current_humidity = _numeric_from(
                status, settings, "currentHumidity", "CurrentHumidity"
            )
        service = first_nested(
            status,
            ("hvacService", "remaining"),
            ("service", "remaining"),
        )
        raw_mode = _normalize_enum(
            first_value(
                first_present(settings, "mode", "ModeId"),
                first_present(status, "mode", "ModeId"),
            ),
            _MODE_BY_ID,
        )
        raw_fan = _normalize_fan(
            first_value(
                first_present(settings, "fan", "FanId"),
                first_present(status, "fan", "FanId"),
            )
        )
        equipment_status = _normalized_status(first_present(status, "equipmentStatus"))
        heating_status = _normalized_status(first_present(status, "heatingStatus"))
        cooling_status = _normalized_status(first_present(status, "coolingStatus"))
        fan_on = coerce_bool(first_present(status, "isFanOn"))
        return NormalizedThermostatZoneState(
            zone_key=zone,
            settings_key=settings_key,
            temperature_unit=_native_temperature_unit(record, settings, status),
            raw_mode=raw_mode,
            raw_fan=raw_fan,
            raw_hold_type=_normalize_enum(
                first_value(
                    first_present(settings, "holdType", "HoldType"),
                    first_present(status, "holdType", "HoldType"),
                ),
                _HOLD_BY_ID,
            ),
            current_temperature=current_temperature,
            current_humidity=current_humidity,
            heat_setpoint=_numeric_from(
                settings, status, "heatSetpoint", "HeatSetpoint", "heat"
            ),
            cool_setpoint=_numeric_from(
                settings, status, "coolSetpoint", "CoolSetpoint", "cool"
            ),
            equipment_status=equipment_status,
            heating_status=heating_status,
            cooling_status=cooling_status,
            fan_on=fan_on,
            operating_state=_derive_operating_state(
                raw_mode=raw_mode,
                equipment_status=equipment_status,
                heating_status=heating_status,
                cooling_status=cooling_status,
                fan_on=fan_on,
            ),
            hvac_service_remaining=coerce_int(
                first_value(
                    first_present(status, "hvacServiceRemaining"),
                    service,
                )
            ),
            outdoor_temperature=_numeric_from(
                status, settings, "outdoorTemperature", "OutdoorTemperature"
            ),
            outdoor_humidity=_numeric_from(
                status, settings, "outdoorHumidity", "OutdoorHumidity"
            ),
        )

    def capabilities(self, record: DeviceRecord) -> DeviceCapabilities:
        """Return stable zone/entity and evidence-gated command capabilities."""
        zones = thermostat_zone_keys_for_record(record)
        normalized = self.normalize(record)
        dynamic = []
        for zone in zones:
            dynamic.extend(_zone_dynamic_sensor_keys(record, zone))
        dynamic.extend(_iaq_dynamic_sensor_keys(record, normalized))

        attached = normalized.attached_humidifier
        attached_sensors, binary = _attached_humidifier_entity_keys(record, attached)
        dynamic.extend(attached_sensors)
        entities = ProfileEntitySet(
            climate_keys=tuple(f"thermostat_{zone.lower()}" for zone in zones),
            humidifier_keys=("attached_humidifier",) if attached is not None else (),
            dynamic_sensor_keys=tuple(dynamic),
            binary_sensor_keys=tuple(binary),
        )
        command_types = (
            CommandType.THERMOSTAT_MODE,
            CommandType.THERMOSTAT_SETPOINTS,
            CommandType.THERMOSTAT_FAN,
            CommandType.THERMOSTAT_HOLD,
            CommandType.ATTACHED_HUMIDIFIER_POWER,
            CommandType.ATTACHED_HUMIDIFIER_TARGET,
        )
        commands = {
            command_type: self._command_capability(
                record, command_type, zones, attached
            )
            for command_type in command_types
        }
        optional_equipment = tuple(
            kind
            for kind, (status_key, _, payload_keys) in THERMOSTAT_IAQ_EQUIPMENT.items()
            if _explicit_equipment_installation(record, payload_keys, status_key) is True
        )
        return DeviceCapabilities(
            profile_key=self.key,
            state_family="thermostat",
            entities=entities,
            commands=commands,
            optional_equipment=optional_equipment,
        )

    def entity_descriptions(self, record: DeviceRecord) -> ProfileEntitySet:
        """Return stable entity keys."""
        return self.capabilities(record).entities

    def _command_capability(
        self,
        record: DeviceRecord,
        command_type: CommandType,
        zones: tuple[str, ...],
        attached: NormalizedAttachedHumidifierState | None,
    ) -> CommandCapability:
        """Build one access- and evidence-aware command capability."""
        access_reason = (
            None if record.hierarchy.access == "manage" else "account_access_read_only"
        )
        model_confirmed = _thermostat_model(record) in _CONFIRMED_8920W_MODELS
        available = False
        reason = "command_contract_unavailable"
        allowed: tuple[str, ...] = ()
        minimum: float | None = None
        maximum: float | None = None
        unit: str | None = None
        evidence = EvidenceLevel.LIVE_CONFIRMED
        if command_type is CommandType.THERMOSTAT_MODE:
            available = model_confirmed and any(
                any(key in _zone_mapping(record, zone)[1] for key in ("mode", "ModeId"))
                for zone in zones
            )
            allowed = _CONFIRMED_8920W_MODES if available else ()
        elif command_type is CommandType.THERMOSTAT_FAN:
            available = model_confirmed and any(
                any(key in _zone_mapping(record, zone)[1] for key in ("fan", "FanId"))
                for zone in zones
            )
            allowed = _CONFIRMED_8920W_FANS if available else ()
        elif command_type is CommandType.THERMOSTAT_HOLD:
            available = model_confirmed and any(
                any(
                    key in _zone_mapping(record, zone)[1]
                    for key in ("holdType", "HoldType")
                )
                for zone in zones
            )
            allowed = _CONFIRMED_8920W_HOLDS if available else ()
        elif command_type is CommandType.THERMOSTAT_SETPOINTS:
            # Community captures establish readable heat/cool keys, but not the
            # PATCH unit and deadband contract. Keep the write disabled until
            # both are captured and manually validated.
            reason = "temperature_patch_contract_unconfirmed"
            evidence = EvidenceLevel.UNKNOWN
        elif command_type is CommandType.ATTACHED_HUMIDIFIER_POWER:
            settings = record.effective_device_settings.get("humidifier")
            available = (
                attached is not None
                and isinstance(settings, dict)
                and settings.get("mode") in {"on", "off"}
            )
        elif command_type is CommandType.ATTACHED_HUMIDIFIER_TARGET:
            settings = record.effective_device_settings.get("humidifier")
            available = (
                attached is not None
                and isinstance(settings, dict)
                and "humiditySetpoint" in settings
            )
            # Issue 8 live-confirmed that the cloud rejects values above 50.
            maximum = 50
            unit = "%RH"
        return CommandCapability(
            type=command_type,
            evidence=evidence,
            writable=available and access_reason is None,
            unavailable_reason=access_reason or (None if available else reason),
            minimum=minimum,
            maximum=maximum,
            unit=unit,
            allowed_values=allowed,
        )

    def encode_command(
        self,
        record: DeviceRecord,
        command: DeviceCommand,
    ) -> EncodedCommand:
        """Validate and encode a thermostat-family command."""
        capability = self.capabilities(record).commands.get(command.type)
        if capability is None or not capability.writable:
            if record.hierarchy.access != "manage":
                raise CommandAccessError("manage access is required")
            raise CommandNotSupportedError(
                capability.unavailable_reason if capability else "unsupported command"
            )
        payload: dict[str, Any]
        if isinstance(command, SetAttachedHumidifierPower):
            payload = {"humidifier": {"mode": "on" if command.enabled else "off"}}
        elif isinstance(command, SetAttachedHumidifierTarget):
            self._validate_numeric(command.humidity, capability)
            payload = {"humidifier": {"humiditySetpoint": command.humidity}}
        elif isinstance(
            command,
            (
                SetThermostatMode,
                SetThermostatSetpoints,
                SetThermostatFan,
                SetThermostatHold,
            ),
        ):
            zone_payload = self._encode_zone_command(record, command, capability)
            payload = {
                thermostat_status_key_for_zone(command.zone): zone_payload
            }
        else:
            raise CommandNotSupportedError("command belongs to another profile")
        return EncodedCommand(payload=payload, command=command)

    def _encode_zone_command(
        self,
        record: DeviceRecord,
        command: SetThermostatMode
        | SetThermostatSetpoints
        | SetThermostatFan
        | SetThermostatHold,
        capability: CommandCapability,
    ) -> dict[str, Any]:
        """Encode one validated zone command in the observed key style."""
        if command.zone not in thermostat_zone_keys_for_record(record):
            raise CommandNotSupportedError("unknown thermostat zone")
        _, settings, _ = _zone_mapping(record, command.zone)
        if isinstance(command, SetThermostatMode):
            return self._encode_enum_setting(
                settings,
                value=command.mode,
                allowed_values=capability.allowed_values,
                codec=_MODE_CODEC,
            )
        if isinstance(command, SetThermostatFan):
            return self._encode_enum_setting(
                settings,
                value=command.mode,
                allowed_values=capability.allowed_values,
                codec=(
                    _FAN_GS_CODEC
                    if _thermostat_model(record) == "8920W_GS"
                    else _FAN_CODEC
                ),
            )
        if isinstance(command, SetThermostatHold):
            return self._encode_enum_setting(
                settings,
                value=command.hold,
                allowed_values=capability.allowed_values,
                codec=_HOLD_CODEC,
            )
        raise CommandNotSupportedError("temperature patch contract is unconfirmed")

    @staticmethod
    def _encode_enum_setting(
        settings: dict[str, Any],
        *,
        value: str,
        allowed_values: tuple[str, ...],
        codec: _EnumSettingCodec,
    ) -> dict[str, Any]:
        """Encode one confirmed enum using the device's observed key style."""
        if value not in allowed_values:
            raise CommandValidationError(f"unsupported {codec.label}")
        if codec.string_key in settings:
            return {codec.string_key: codec.string_values.get(value, value)}
        if codec.numeric_key in settings:
            return {codec.numeric_key: codec.numeric_values[value]}
        raise CommandNotSupportedError(f"zone {codec.label} contract is unavailable")

    def command_confirmed(
        self,
        record: DeviceRecord,
        command: DeviceCommand,
    ) -> bool:
        """Compare normalized confirmed state with numeric tolerance."""
        normalized = self.normalize(replace(record, pending_device_settings={}))
        confirmed = False
        if isinstance(command, SetAttachedHumidifierPower):
            state = normalized.attached_humidifier
            confirmed = state is not None and state.mode == (
                "on" if command.enabled else "off"
            )
        elif isinstance(command, SetAttachedHumidifierTarget):
            state = normalized.attached_humidifier
            confirmed = state is not None and state.target_humidity == command.humidity
        elif isinstance(
            command,
            (
                SetThermostatMode,
                SetThermostatSetpoints,
                SetThermostatFan,
                SetThermostatHold,
            ),
        ):
            zone = normalized.zones.get(command.zone)
            if zone is not None:
                if isinstance(command, SetThermostatMode):
                    confirmed = zone.raw_mode == command.mode
                elif isinstance(command, SetThermostatFan):
                    confirmed = zone.raw_fan == command.mode
                elif isinstance(command, SetThermostatHold):
                    confirmed = zone.raw_hold_type == command.hold
        return confirmed

    def _installed_iaq_status_requests(
        self,
        record: DeviceRecord,
    ) -> tuple[tuple[str, str], ...]:
        """Return endpoints only for explicitly installed equipment."""
        return tuple(
            (status_key, endpoint)
            for _, (status_key, endpoint, payload_keys) in THERMOSTAT_IAQ_EQUIPMENT.items()
            if _explicit_equipment_installation(record, payload_keys, status_key) is True
        )

    def _normalize_iaq(
        self,
        record: DeviceRecord,
    ) -> dict[str, NormalizedThermostatIAQState]:
        """Normalize observed attached IAQ status payloads."""
        normalized: dict[str, NormalizedThermostatIAQState] = {}
        for kind, (status_key, _, _) in THERMOSTAT_IAQ_EQUIPMENT.items():
            payload = record.status_payloads.get(status_key)
            if not isinstance(payload, dict):
                continue
            service = first_nested(
                payload,
                ("filterService", "remaining"),
                ("waterPanelService", "remaining"),
            )
            needs_service = first_nested(
                payload,
                ("filterService", "needsService"),
                ("waterPanelService", "needsService"),
            )
            normalized[kind] = NormalizedThermostatIAQState(
                kind=kind,
                status=_normalized_status(first_present(payload, "equipmentStatus")),
                service_remaining=coerce_int(
                    first_value(first_present(payload, "serviceRemaining"), service)
                ),
                needs_service=coerce_bool(
                    first_value(first_present(payload, "needsService"), needs_service)
                ),
            )
        return normalized

    def _normalize_attached_humidifier(
        self,
        record: DeviceRecord,
        zones: dict[str, NormalizedThermostatZoneState],
    ) -> NormalizedAttachedHumidifierState | None:
        """Normalize the global attached humidifier without assigning a zone."""
        status_key, _, payload_keys = THERMOSTAT_IAQ_EQUIPMENT["humidifier"]
        if _explicit_equipment_installation(record, payload_keys, status_key) is not True:
            return None
        settings = record.effective_device_settings.get("humidifier")
        settings = settings if isinstance(settings, dict) else {}
        status = record.status_payloads.get(status_key)
        status = status if isinstance(status, dict) else {}
        panel = status.get("waterPanelService")
        panel = panel if isinstance(panel, dict) else {}
        current_humidity = select_sensor_reading(status.get("humSensors"))
        if current_humidity is None:
            current_humidity = coerce_float(first_present(status, "currentHumidity"))
        if current_humidity is None and len(zones) == 1:
            current_humidity = next(iter(zones.values())).current_humidity
        return NormalizedAttachedHumidifierState(
            installed=True,
            mode=(
                normalize_string(settings.get("mode"))
                if "mode" in settings
                else None
            ),
            current_humidity=current_humidity,
            target_humidity=coerce_int(settings.get("humiditySetpoint")),
            equipment_status=_normalized_status(
                first_present(status, "equipmentStatus")
            ),
            water_panel_remaining=coerce_int(panel.get("remaining")),
            water_panel_needs_service=coerce_bool(panel.get("needsService")),
        )

    @staticmethod
    def _validate_numeric(value: float, capability: CommandCapability) -> None:
        """Validate finite explicit numeric constraints."""
        number = coerce_float(value)
        if number is None:
            raise CommandValidationError("setpoint must be finite")
        if capability.minimum is not None and number < capability.minimum:
            raise CommandValidationError("setpoint is below the supported minimum")
        if capability.maximum is not None and number > capability.maximum:
            raise CommandValidationError("setpoint is above the supported maximum")
