"""Shared profile, capability, and command contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from ..models import DeviceRecord


class EvidenceLevel(StrEnum):
    """Protocol evidence strength."""

    LIVE_CONFIRMED = "live_confirmed"
    CAPTURED = "captured"
    DECOMPILED = "decompiled"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class CommandType(StrEnum):
    """Normalized write operations understood by profiles."""

    DEHUMIDIFIER_POWER = "dehumidifier_power"
    DEHUMIDIFIER_TARGET = "dehumidifier_target"
    DEHUMIDIFIER_HIGH_HUMIDITY_ALERT = "dehumidifier_high_humidity_alert"
    THERMOSTAT_MODE = "thermostat_mode"
    THERMOSTAT_SETPOINTS = "thermostat_setpoints"
    THERMOSTAT_FAN = "thermostat_fan"
    THERMOSTAT_HOLD = "thermostat_hold"
    ATTACHED_HUMIDIFIER_POWER = "attached_humidifier_power"
    ATTACHED_HUMIDIFIER_TARGET = "attached_humidifier_target"


@dataclass(frozen=True, slots=True)
class SetDehumidifierPower:
    """Enable or disable a standalone dehumidifier."""

    enabled: bool
    type: CommandType = field(init=False, default=CommandType.DEHUMIDIFIER_POWER)


@dataclass(frozen=True, slots=True)
class SetDehumidifierTarget:
    """Set a standalone dehumidifier humidity target."""

    humidity: int
    type: CommandType = field(init=False, default=CommandType.DEHUMIDIFIER_TARGET)


@dataclass(frozen=True, slots=True)
class SetHighHumidityAlert:
    """Set the high-humidity alert threshold."""

    humidity: int
    type: CommandType = field(
        init=False,
        default=CommandType.DEHUMIDIFIER_HIGH_HUMIDITY_ALERT,
    )


@dataclass(frozen=True, slots=True)
class SetThermostatMode:
    """Set a thermostat zone HVAC mode."""

    zone: str
    mode: str
    type: CommandType = field(init=False, default=CommandType.THERMOSTAT_MODE)


@dataclass(frozen=True, slots=True)
class SetThermostatSetpoints:
    """Set one or both thermostat zone setpoints."""

    zone: str
    heat: float | None = None
    cool: float | None = None
    type: CommandType = field(init=False, default=CommandType.THERMOSTAT_SETPOINTS)


@dataclass(frozen=True, slots=True)
class SetThermostatFan:
    """Set a thermostat zone fan mode."""

    zone: str
    mode: str
    type: CommandType = field(init=False, default=CommandType.THERMOSTAT_FAN)


@dataclass(frozen=True, slots=True)
class SetThermostatHold:
    """Set a thermostat zone hold mode."""

    zone: str
    hold: str
    type: CommandType = field(init=False, default=CommandType.THERMOSTAT_HOLD)


@dataclass(frozen=True, slots=True)
class SetAttachedHumidifierPower:
    """Enable or disable a thermostat-attached humidifier."""

    enabled: bool
    type: CommandType = field(init=False, default=CommandType.ATTACHED_HUMIDIFIER_POWER)


@dataclass(frozen=True, slots=True)
class SetAttachedHumidifierTarget:
    """Set the target of a thermostat-attached humidifier."""

    humidity: int
    type: CommandType = field(init=False, default=CommandType.ATTACHED_HUMIDIFIER_TARGET)


type DeviceCommand = (
    SetDehumidifierPower
    | SetDehumidifierTarget
    | SetHighHumidityAlert
    | SetThermostatMode
    | SetThermostatSetpoints
    | SetThermostatFan
    | SetThermostatHold
    | SetAttachedHumidifierPower
    | SetAttachedHumidifierTarget
)


@dataclass(frozen=True, slots=True)
class CommandCapability:
    """Availability and constraints for one normalized command."""

    type: CommandType
    evidence: EvidenceLevel
    writable: bool
    required_access: str = "manage"
    unavailable_reason: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    unit: str | None = None
    allowed_values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProfileEntitySet:
    """Stable entity keys exposed by a profile."""

    climate_keys: tuple[str, ...] = ()
    humidifier_keys: tuple[str, ...] = ()
    switch_keys: tuple[str, ...] = ()
    sensor_keys: tuple[str, ...] = ()
    dynamic_sensor_keys: tuple[str, ...] = ()
    binary_sensor_keys: tuple[str, ...] = ()
    number_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeviceCapabilities:
    """Granular capabilities for one recognized device."""

    profile_key: str
    state_family: str
    entities: ProfileEntitySet
    commands: dict[CommandType, CommandCapability] = field(default_factory=dict)
    optional_equipment: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProfileStatusRequest:
    """A profile-owned REST status endpoint."""

    key: str
    endpoint: str


@dataclass(frozen=True, slots=True)
class EncodedCommand:
    """A validated command encoded at the vendor boundary."""

    payload: dict[str, Any]
    command: DeviceCommand


class AprilaireCommandError(Exception):
    """Base local command validation failure."""


class CommandAccessError(AprilaireCommandError):
    """The hierarchy does not grant manage access."""


class CommandNotSupportedError(AprilaireCommandError):
    """The profile does not advertise the requested command."""


class CommandValidationError(AprilaireCommandError):
    """A command value violates profile constraints."""


class DeviceProfile(Protocol):
    """Interface implemented by each device-family profile."""

    key: str

    def unsupported_reason(self, record: DeviceRecord) -> str | None:
        """Return why this profile cannot yet recognize a record."""
        ...

    def matches(self, record: DeviceRecord) -> bool:
        """Return whether the profile recognizes the record."""
        ...

    def status_requests(self, record: DeviceRecord) -> tuple[ProfileStatusRequest, ...]:
        """Return independently optional REST status requests."""
        ...

    def has_required_status(self, record: DeviceRecord) -> bool:
        """Return whether required profile status has been loaded."""
        ...

    def normalize(self, record: DeviceRecord) -> object | None:
        """Normalize vendor state for entities."""
        ...

    def capabilities(self, record: DeviceRecord) -> DeviceCapabilities:
        """Return stable entities and granular command capabilities."""
        ...

    def entity_descriptions(self, record: DeviceRecord) -> ProfileEntitySet:
        """Return stable entity keys."""
        ...

    def encode_command(
        self,
        record: DeviceRecord,
        command: DeviceCommand,
    ) -> EncodedCommand:
        """Validate and encode a domain command."""
        ...

    def command_confirmed(
        self,
        record: DeviceRecord,
        command: DeviceCommand,
    ) -> bool:
        """Return whether confirmed normalized state matches a command."""
        ...

    def mismatch_is_rejection(self, command: DeviceCommand) -> bool:
        """Return whether a newer complete mismatch decisively rejects a command."""
        ...
