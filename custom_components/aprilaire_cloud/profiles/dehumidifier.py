"""Standalone AprilAire dehumidifier profile."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, cast

from ..const import DEHUMIDIFIER_REPORTING_TYPE, DEHUMIDIFIER_SCALE
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
    SetDehumidifierPower,
    SetDehumidifierTarget,
    SetHighHumidityAlert,
)
from .common import coerce_bool, coerce_float, coerce_int, select_sensor_reading


@dataclass(frozen=True, slots=True)
class NormalizedTemperatureProbe:
    """A non-controlling temperature probe."""

    uid: int
    name: str
    reading: float | None


@dataclass(frozen=True, slots=True)
class NormalizedDehumidifierState:
    """Normalized standalone dehumidifier state."""

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
    high_humidity_alert_limit: int | None = None
    extra_temperature_probes: tuple[NormalizedTemperatureProbe, ...] = ()


def _settings(record: DeviceRecord) -> dict[str, Any]:
    """Return the standalone dehumidifier settings object."""
    value = record.effective_device_settings.get("dehumidifier")
    return value if isinstance(value, dict) else {}


def _setup(record: DeviceRecord) -> dict[str, Any]:
    """Return the standalone dehumidifier setup object."""
    value = record.device_setup.get("dehumidifier")
    return value if isinstance(value, dict) else {}


def _status(record: DeviceRecord) -> dict[str, Any]:
    """Return the standalone status payload."""
    return record.status_payloads.get("dehumidifier", {})


def _write_reason(record: DeviceRecord) -> str | None:
    """Return the account-level reason writes are unavailable."""
    return None if record.hierarchy.access == "manage" else "account_access_read_only"


def _capability(
    command_type: CommandType,
    *,
    available: bool,
    record: DeviceRecord,
    reason: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> CommandCapability:
    """Build a live-confirmed command capability."""
    access_reason = _write_reason(record)
    return CommandCapability(
        type=command_type,
        evidence=EvidenceLevel.LIVE_CONFIRMED,
        writable=available and access_reason is None,
        unavailable_reason=access_reason or (None if available else reason),
        minimum=minimum,
        maximum=maximum,
        unit="%RH" if minimum is not None else None,
    )


class DehumidifierProfile:
    """Recognition, normalization, capabilities, and writes for dehumidifiers."""

    key = "dehumidifier"
    _status_requests = (ProfileStatusRequest("dehumidifier", "dehumidifier"),)

    def unsupported_reason(self, record: DeviceRecord) -> str | None:
        """Recognize the family independently of its control mode."""
        if record.device_setup.get("type") == DEHUMIDIFIER_REPORTING_TYPE:
            if not _setup(record):
                return "awaiting_device_setup"
            if not _settings(record):
                return "awaiting_device_settings"
            return None
        if "dehumidifier" in record.device_settings and not record.device_setup:
            return "awaiting_device_setup"
        return "unsupported_equipment_type"

    def matches(self, record: DeviceRecord) -> bool:
        """Return whether this is a understood standalone dehumidifier."""
        return self.unsupported_reason(record) is None

    def status_requests(self, record: DeviceRecord) -> tuple[ProfileStatusRequest, ...]:
        """Return the observed standalone status endpoint."""
        return self._status_requests

    def has_required_status(self, record: DeviceRecord) -> bool:
        """Return whether status data has been loaded."""
        return "dehumidifier" in record.status_payloads

    def normalize(self, record: DeviceRecord) -> NormalizedDehumidifierState:
        """Normalize observed E100W-compatible fields."""
        settings = _settings(record)
        status = _status(record)
        alerts = status.get("alerts")
        alerts = alerts if isinstance(alerts, dict) else {}
        humidity_sensors = status.get("humSensors")
        temperature_sensors = status.get("tempSensors")
        humidity_sensors = humidity_sensors if isinstance(humidity_sensors, list) else []
        temperature_sensors = (
            temperature_sensors if isinstance(temperature_sensors, list) else []
        )

        current_humidity = select_sensor_reading(humidity_sensors)
        current_temperature = select_sensor_reading(temperature_sensors)

        sensor_names = {
            sensor.get("uid"): sensor.get("dispName")
            for sensor in settings.get("sensors", [])
            if isinstance(sensor, dict)
            and sensor.get("uid") is not None
            and isinstance(sensor.get("dispName"), str)
        }
        probes: list[NormalizedTemperatureProbe] = []
        for sensor in temperature_sensors:
            if (
                not isinstance(sensor, dict)
                or sensor.get("isControlling") is True
                or coerce_int(sensor.get("uid")) is None
            ):
                continue
            uid = cast(int, coerce_int(sensor.get("uid")))
            probes.append(
                NormalizedTemperatureProbe(
                    uid=uid,
                    name=str(sensor_names.get(uid, f"Temperature {uid}")),
                    reading=coerce_float(sensor.get("reading")),
                )
            )

        alert_limits_value = settings.get("alertLimits")
        alert_limits_value = (
            alert_limits_value if isinstance(alert_limits_value, dict) else {}
        )
        filter_service = status.get("filterService")
        filter_service = filter_service if isinstance(filter_service, dict) else {}
        return NormalizedDehumidifierState(
            mode=settings.get("mode") if isinstance(settings.get("mode"), str) else None,
            current_humidity=current_humidity,
            target_humidity=coerce_int(settings.get("humiditySetpoint")),
            current_temperature=current_temperature,
            equipment_status=(
                status.get("equipmentStatus")
                if isinstance(status.get("equipmentStatus"), str)
                else None
            ),
            filter_remaining=coerce_int(filter_service.get("remaining")),
            filter_needs_service=coerce_bool(filter_service.get("needsService")),
            fan_runtime_hours=coerce_int(status.get("fanTimeHours")),
            wifi_rssi=coerce_int(status.get("wifiRSSI")),
            alert_high_humidity=coerce_bool(alerts.get("highHum")),
            alert_low_humidity=coerce_bool(alerts.get("lowHum")),
            alert_high_temperature=coerce_bool(alerts.get("highTemp")),
            alert_low_temperature=coerce_bool(alerts.get("lowTemp")),
            compressor_on=coerce_bool(status.get("isCompOn")),
            dehumidifier_fan_on=coerce_bool(status.get("isDehumFanOn")),
            hvac_fan_on=coerce_bool(status.get("isHvacFanOn")),
            high_humidity_alert_limit=coerce_int(alert_limits_value.get("highHum")),
            extra_temperature_probes=tuple(probes),
        )

    def capabilities(self, record: DeviceRecord) -> DeviceCapabilities:
        """Return stable entities and command availability."""
        settings = _settings(record)
        setup = _setup(record)
        status = _status(record)
        alerts = status.get("alerts")
        alerts = alerts if isinstance(alerts, dict) else {}
        filter_service = status.get("filterService")
        filter_service = filter_service if isinstance(filter_service, dict) else {}
        internal_percent = (
            setup.get("controlType") == "internal"
            and setup.get("scale") == DEHUMIDIFIER_SCALE
            and "drynessSetpoint" not in settings
        )
        can_power = (
            record.hierarchy.access == "manage"
            and settings.get("mode") in {"on", "off"}
        )
        can_target = (
            record.hierarchy.access == "manage"
            and internal_percent
            and "humiditySetpoint" in settings
        )
        can_alert = (
            record.hierarchy.access == "manage"
            and internal_percent
            and isinstance(settings.get("alertLimits"), dict)
            and "highHum" in settings["alertLimits"]
        )

        sensor_keys = ["current_humidity", "current_temperature"]
        if "remaining" in filter_service:
            sensor_keys.append("filter_life")
        if "fanTimeHours" in status:
            sensor_keys.append("fan_runtime")
        if "wifiRSSI" in status:
            sensor_keys.append("wifi_signal")
        if "equipmentStatus" in status:
            sensor_keys.append("equipment_status")
        binary_keys = []
        if "needsService" in filter_service:
            binary_keys.append("filter_service")
        binary_keys.extend(
            key
            for field, key in (
                ("highHum", "alert_high_humidity"),
                ("lowHum", "alert_low_humidity"),
                ("highTemp", "alert_high_temperature"),
                ("lowTemp", "alert_low_temperature"),
            )
            if field in alerts
        )
        binary_keys.extend(
            key
            for field, key in (
                ("isCompOn", "compressor"),
                ("isDehumFanOn", "dehumidifier_fan"),
                ("isHvacFanOn", "hvac_fan"),
            )
            if field in status
        )
        entities = ProfileEntitySet(
            humidifier_keys=("dehumidifier",) if can_power and can_target else (),
            switch_keys=(
                ("dehumidifier_power",)
                if can_power and not can_target
                else ()
            ),
            sensor_keys=tuple(sensor_keys),
            dynamic_sensor_keys=tuple(
                f"temperature_{probe.uid}"
                for probe in self.normalize(record).extra_temperature_probes
            ),
            binary_sensor_keys=tuple(binary_keys),
            number_keys=(
                ("high_humidity",)
                if can_alert
                else ()
            ),
        )
        commands = {
            CommandType.DEHUMIDIFIER_POWER: _capability(
                CommandType.DEHUMIDIFIER_POWER,
                available=settings.get("mode") in {"on", "off"},
                record=record,
                reason="mode_contract_unavailable",
            ),
            CommandType.DEHUMIDIFIER_TARGET: _capability(
                CommandType.DEHUMIDIFIER_TARGET,
                available=internal_percent and "humiditySetpoint" in settings,
                record=record,
                reason="humidity_target_not_applicable",
                minimum=40,
                maximum=80,
            ),
            CommandType.DEHUMIDIFIER_HIGH_HUMIDITY_ALERT: _capability(
                CommandType.DEHUMIDIFIER_HIGH_HUMIDITY_ALERT,
                available=internal_percent
                and isinstance(settings.get("alertLimits"), dict)
                and "highHum" in settings["alertLimits"],
                record=record,
                reason="alert_limit_contract_unavailable",
                minimum=40,
                maximum=90,
            ),
        }
        return DeviceCapabilities(
            profile_key=self.key,
            state_family="dehumidifier",
            entities=entities,
            commands=commands,
        )

    def entity_descriptions(self, record: DeviceRecord) -> ProfileEntitySet:
        """Return stable entity keys."""
        return self.capabilities(record).entities

    def encode_command(
        self,
        record: DeviceRecord,
        command: DeviceCommand,
    ) -> EncodedCommand:
        """Validate access/constraints and encode an E100W-compatible command."""
        capability = self.capabilities(record).commands.get(command.type)
        if capability is None or not capability.writable:
            if record.hierarchy.access != "manage":
                raise CommandAccessError("manage access is required")
            raise CommandNotSupportedError(
                capability.unavailable_reason if capability else "unsupported command"
            )
        payload: dict[str, Any]
        if isinstance(command, SetDehumidifierPower):
            payload = {"dehumidifier": {"mode": "on" if command.enabled else "off"}}
        elif isinstance(command, SetDehumidifierTarget):
            self._validate_range(command.humidity, capability)
            payload = {"dehumidifier": {"humiditySetpoint": command.humidity}}
        elif isinstance(command, SetHighHumidityAlert):
            self._validate_range(command.humidity, capability)
            payload = {
                "dehumidifier": {"alertLimits": {"highHum": command.humidity}}
            }
        else:
            raise CommandNotSupportedError("command belongs to another profile")
        return EncodedCommand(payload=payload, command=command)

    def command_confirmed(
        self,
        record: DeviceRecord,
        command: DeviceCommand,
    ) -> bool:
        """Compare a command to normalized confirmed state."""
        state = self.normalize(replace(record, pending_device_settings={}))
        if isinstance(command, SetDehumidifierPower):
            return state.mode == ("on" if command.enabled else "off")
        if isinstance(command, SetDehumidifierTarget):
            return state.target_humidity == command.humidity
        if isinstance(command, SetHighHumidityAlert):
            return state.high_humidity_alert_limit == command.humidity
        return False

    @staticmethod
    def _validate_range(value: float, capability: CommandCapability) -> None:
        """Validate an observed numeric constraint."""
        if capability.minimum is not None and value < capability.minimum:
            raise CommandValidationError("value is below the supported minimum")
        if capability.maximum is not None and value > capability.maximum:
            raise CommandValidationError("value is above the supported maximum")
