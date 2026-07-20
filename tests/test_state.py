"""Pure reducer tests for AprilAire Cloud state helpers."""

from __future__ import annotations

import custom_components.aprilaire_cloud.profiles as profiles_module
from custom_components.aprilaire_cloud.models import DeviceRecord
from custom_components.aprilaire_cloud.profiles import (
    ProfileEntitySet,
    ProfileStatusRequest,
    record_has_required_status,
    record_requires_rest_refresh,
    status_requests_for_record,
)
from custom_components.aprilaire_cloud.state import (
    apply_confirmed_device_settings,
    apply_device_event,
    apply_device_message,
    apply_full_device_settings,
    apply_hierarchy,
    apply_pending_device_settings,
    apply_rest_refresh,
    evaluate_device_support,
)

from .common import (
    DEVICE_ID,
    LOCATION_ID,
    SECOND_DEVICE_ID,
    THERMOSTAT_DEVICE_ID,
    build_dehumidifier_status,
    build_device_settings,
    build_device_status,
    build_hierarchy,
    build_iaq_status,
    build_initial_messages,
    build_thermostat_hierarchy,
    build_thermostat_settings,
    build_thermostat_setup,
    build_thermostat_status,
)


def _build_supported_record() -> DeviceRecord:
    """Create a fully supported device record."""
    _, devices, _ = apply_hierarchy(build_hierarchy(), {})
    record = devices[DEVICE_ID]
    for message in build_initial_messages():
        record = evaluate_device_support(apply_device_message(record, message))
    return record


def test_older_websocket_settings_cannot_overwrite_newer_state() -> None:
    """WebSocket updates are ordered by their vendor `asOf` timestamp."""
    _, devices, _ = apply_hierarchy(build_hierarchy(), {})
    record = devices[DEVICE_ID]
    newer = build_device_settings(humidity=61)
    newer["asOf"] = "2026-03-24T00:10:00.000Z"
    older = build_device_settings(humidity=41)
    older["asOf"] = "2026-03-24T00:09:00.000Z"

    record = apply_device_message(record, newer)
    record = apply_device_message(record, older)

    assert record.device_settings["dehumidifier"]["humiditySetpoint"] == 61


def test_rest_ordering_and_equal_timestamp_source_precedence() -> None:
    """Stale/equal REST cannot beat push, while newer REST can reconcile it."""
    _, devices, _ = apply_hierarchy(build_hierarchy(), {})
    websocket = build_device_settings(humidity=61)
    websocket["asOf"] = "2026-03-24T00:10:00.000Z"
    record = apply_device_message(devices[DEVICE_ID], websocket)

    older_rest = build_device_settings(humidity=41)
    older_rest["asOf"] = "2026-03-24T00:09:00.000Z"
    equal_rest = build_device_settings(humidity=42)
    equal_rest["asOf"] = websocket["asOf"]
    newer_rest = build_device_settings(humidity=62)
    newer_rest["asOf"] = "2026-03-24T00:11:00.000Z"

    record = apply_full_device_settings(record, older_rest)
    record = apply_full_device_settings(record, equal_rest)
    assert record.device_settings["dehumidifier"]["humiditySetpoint"] == 61

    record = apply_full_device_settings(record, newer_rest)
    assert record.device_settings["dehumidifier"]["humiditySetpoint"] == 62


def test_partial_settings_deep_merge_preserves_falsy_values_and_unrelated_fields() -> None:
    """Push settings are deltas; false, zero, and empty values are data."""
    _, devices, _ = apply_hierarchy(build_hierarchy(), {})
    initial = build_device_settings(humidity=52)
    record = apply_device_message(devices[DEVICE_ID], initial)
    partial = {
        "_type": "DeviceSettings",
        "deviceId": DEVICE_ID,
        "asOf": "2026-03-24T00:10:00.000Z",
        "dehumidifier": {
            "mode": "off",
            "forceHvacFan": False,
            "sampleRate": 0,
            "sensors": [],
        },
    }

    record = apply_device_message(record, partial)

    dehumidifier = record.device_settings["dehumidifier"]
    assert dehumidifier["humiditySetpoint"] == 52
    assert dehumidifier["forceHvacFan"] is False
    assert dehumidifier["sampleRate"] == 0
    assert dehumidifier["sensors"] == []


def test_offline_and_rescinded_events_are_timestamp_ordered() -> None:
    """A newer health event wins; a rescinded event marks recovery."""
    _, devices, _ = apply_hierarchy(build_thermostat_hierarchy(), {})
    record = devices[THERMOSTAT_DEVICE_ID]
    offline = {
        "_type": "DeviceEvent",
        "deviceId": THERMOSTAT_DEVICE_ID,
        "type": "offline",
        "occurred": "2026-03-24T00:10:00.000Z",
    }
    stale_recovery = {
        **offline,
        "occurred": "2026-03-24T00:08:00.000Z",
        "rescinded": "2026-03-24T00:09:00.000Z",
    }
    recovery = {
        **offline,
        "rescinded": "2026-03-24T00:11:00.000Z",
    }

    record = apply_device_event(record, offline)
    assert record.health.offline is True
    record = apply_device_event(record, stale_recovery)
    assert record.health.offline is True
    record = apply_device_event(record, recovery)
    assert record.health.offline is False
    record = apply_device_event(record, offline)
    assert record.health.offline is False


def test_apply_hierarchy_tracks_removed_devices() -> None:
    """Hierarchy refreshes should preserve surviving devices and report removals."""
    locations, devices, removed_ids = apply_hierarchy(
        build_hierarchy(include_second_device=True), {}
    )

    assert set(locations) == {LOCATION_ID}
    assert set(devices) == {DEVICE_ID, SECOND_DEVICE_ID}
    assert removed_ids == set()

    _, updated_devices, removed_ids = apply_hierarchy(build_hierarchy(), devices)

    assert set(updated_devices) == {DEVICE_ID}
    assert removed_ids == {SECOND_DEVICE_ID}


def test_dryness_setpoint_disables_only_inapplicable_target_writes() -> None:
    """A partial-control dehumidifier remains useful without a target write."""
    record = _build_supported_record()
    settings = build_device_settings()
    settings["dehumidifier"]["drynessSetpoint"] = 5

    updated = evaluate_device_support(apply_confirmed_device_settings(record, settings))

    assert updated.supported is True
    assert updated.profile_key == "dehumidifier"
    profile = profiles_module.get_profile(updated.profile_key)
    assert profile is not None
    capabilities = profile.capabilities(updated)
    assert capabilities.commands[
        profiles_module.CommandType.DEHUMIDIFIER_POWER
    ].writable is True
    target = capabilities.commands[profiles_module.CommandType.DEHUMIDIFIER_TARGET]
    assert target.writable is False
    assert target.unavailable_reason == "humidity_target_not_applicable"


def test_status_messages_populate_generic_status() -> None:
    """Known status messages should populate generic status storage."""
    _, devices, _ = apply_hierarchy(build_hierarchy(), {})
    status = build_dehumidifier_status(humidity=51)

    updated = apply_device_message(devices[DEVICE_ID], status)

    assert updated.status_payloads["dehumidifier"] == status


def test_dehumidifier_profile_exposes_status_requirements() -> None:
    """The dehumidifier profile should advertise its existing REST status endpoint."""
    _, devices, _ = apply_hierarchy(build_hierarchy(), {})
    record = apply_device_message(devices[DEVICE_ID], build_device_settings())

    assert [
        (request.key, request.endpoint) for request in status_requests_for_record(record)
    ] == [("dehumidifier", "dehumidifier")]
    assert record_has_required_status(record) is False

    record = apply_device_message(record, build_dehumidifier_status())

    assert record_has_required_status(record) is True


def test_terminally_rejected_profiles_do_not_contribute_status_requests() -> None:
    """Profiles that cannot support a record should not force REST status reads."""
    _, devices, _ = apply_hierarchy(build_hierarchy(), {})
    record = apply_device_message(
        devices[DEVICE_ID],
        {"_type": "DeviceSetup", "deviceId": DEVICE_ID, "type": "ventilator"},
    )

    assert status_requests_for_record(record) == ()
    assert record_has_required_status(record) is True
    assert record_requires_rest_refresh(record) is False
    assert record_requires_rest_refresh(record, location_unhealthy=True) is False


def test_pending_second_profile_contributes_only_its_status_requests(monkeypatch) -> None:
    """Rejected profiles should be skipped while pending profiles request their data."""

    class PendingProfile:
        key = "pending"
        supported_writes: tuple[str, ...] = ()

        def unsupported_reason(self, record: DeviceRecord) -> str | None:
            return "awaiting_device_settings"

        def matches(self, record: DeviceRecord) -> bool:
            return False

        def status_requests(self, record: DeviceRecord) -> tuple[ProfileStatusRequest, ...]:
            return (ProfileStatusRequest(key="pending", endpoint="pending"),)

        def has_required_status(self, record: DeviceRecord) -> bool:
            return "pending" in record.status_payloads

        def normalize(self, record: DeviceRecord) -> object | None:
            return None

        def entity_descriptions(self, record: DeviceRecord) -> ProfileEntitySet:
            return ProfileEntitySet()

    monkeypatch.setattr(
        profiles_module,
        "DEVICE_PROFILES",
        (profiles_module.DEHUMIDIFIER_PROFILE, PendingProfile()),
    )
    _, devices, _ = apply_hierarchy(build_hierarchy(), {})
    record = apply_device_message(
        devices[DEVICE_ID],
        {"_type": "DeviceSetup", "deviceId": DEVICE_ID, "type": "ventilator"},
    )

    assert [
        (request.key, request.endpoint) for request in status_requests_for_record(record)
    ] == [("pending", "pending")]
    assert record_has_required_status(record) is False
    assert record_requires_rest_refresh(record) is True


def test_supported_profile_contributes_only_its_status_requests(monkeypatch) -> None:
    """Supported profile selection should avoid sibling profile status requests."""

    class OtherProfile:
        key = "other"
        supported_writes: tuple[str, ...] = ()

        def unsupported_reason(self, record: DeviceRecord) -> str | None:
            return "awaiting_device_settings"

        def matches(self, record: DeviceRecord) -> bool:
            return False

        def status_requests(self, record: DeviceRecord) -> tuple[ProfileStatusRequest, ...]:
            return (ProfileStatusRequest(key="other", endpoint="other"),)

        def has_required_status(self, record: DeviceRecord) -> bool:
            return False

        def normalize(self, record: DeviceRecord) -> object | None:
            return None

        def entity_descriptions(self, record: DeviceRecord) -> ProfileEntitySet:
            return ProfileEntitySet()

    monkeypatch.setattr(
        profiles_module,
        "DEVICE_PROFILES",
        (profiles_module.DEHUMIDIFIER_PROFILE, OtherProfile()),
    )
    record = _build_supported_record()

    assert [
        (request.key, request.endpoint) for request in status_requests_for_record(record)
    ] == [("dehumidifier", "dehumidifier")]
    assert record_requires_rest_refresh(record) is False
    assert record_requires_rest_refresh(record, location_unhealthy=True) is True


def test_pending_settings_overlay_ignores_stale_confirmation() -> None:
    """A stale remote settings payload must not clear a newer local override."""
    record = apply_pending_device_settings(
        _build_supported_record(),
        {"dehumidifier": {"humiditySetpoint": 60}},
    )

    assert record.effective_device_settings["dehumidifier"]["humiditySetpoint"] == 60

    stale_remote = apply_confirmed_device_settings(record, build_device_settings(humidity=52))

    assert stale_remote.device_settings["dehumidifier"]["humiditySetpoint"] == 52
    assert stale_remote.pending_device_settings["dehumidifier"]["humiditySetpoint"] == 60
    assert stale_remote.effective_device_settings["dehumidifier"]["humiditySetpoint"] == 60

    confirmed = apply_confirmed_device_settings(stale_remote, build_device_settings(humidity=60))

    assert confirmed.pending_device_settings == {}
    assert confirmed.effective_device_settings["dehumidifier"]["humiditySetpoint"] == 60


def test_apply_rest_refresh_clears_matching_pending_settings() -> None:
    """REST reconciliation should finalize a pending write when the remote matches."""
    record = apply_pending_device_settings(
        _build_supported_record(),
        {"dehumidifier": {"humiditySetpoint": 58}},
    )

    settings = build_device_settings(humidity=58)
    settings["asOf"] = "2026-03-24T00:10:00.000Z"
    status = build_dehumidifier_status(humidity=51)
    status["asOf"] = "2026-03-24T00:10:00.000Z"
    refreshed = evaluate_device_support(
        apply_rest_refresh(
            record,
            device_status=build_initial_messages()[3],
            settings=settings,
            status_payloads={"dehumidifier": status},
        )
    )

    assert refreshed.pending_device_settings == {}
    assert refreshed.device_settings["dehumidifier"]["humiditySetpoint"] == 58
    assert refreshed.status_payloads["dehumidifier"]["humSensors"][0]["reading"] == 51


def test_apply_rest_refresh_accepts_generic_status_payloads() -> None:
    """REST reconciliation should accept profile-owned status payloads."""
    record = _build_supported_record()

    settings = build_device_settings(humidity=58)
    settings["asOf"] = "2026-03-24T00:10:00.000Z"
    status = build_dehumidifier_status(humidity=51)
    status["asOf"] = "2026-03-24T00:10:00.000Z"
    refreshed = evaluate_device_support(
        apply_rest_refresh(
            record,
            device_status=build_initial_messages()[3],
            settings=settings,
            status_payloads={"dehumidifier": status},
        )
    )

    assert refreshed.status_payloads["dehumidifier"]["humSensors"][0]["reading"] == 51


def test_dehumidifier_rest_settings_are_a_full_replacement() -> None:
    """A REST settings document replaces fields absent from the response."""
    record = _build_supported_record()
    settings = {
        "_type": "DeviceSettings",
        "deviceId": DEVICE_ID,
        "asOf": "2026-03-24T00:10:00.000Z",
        "dehumidifier": {"humiditySetpoint": 58},
    }
    refreshed = evaluate_device_support(
        apply_rest_refresh(
            record,
            device_status=build_device_status(DEVICE_ID),
            settings=settings,
            status_payloads={"dehumidifier": build_dehumidifier_status(humidity=51)},
        )
    )

    assert refreshed.device_settings["dehumidifier"]["humiditySetpoint"] == 58
    assert "alertLimits" not in refreshed.device_settings["dehumidifier"]


def test_thermostat_settings_profile_becomes_supported_without_affecting_dehumidifier() -> None:
    """Thermostat records should classify independently from dehumidifiers."""
    _, devices, _ = apply_hierarchy(build_thermostat_hierarchy(), {})
    record = devices[THERMOSTAT_DEVICE_ID]
    for message in [
        build_thermostat_setup(),
        build_thermostat_settings(),
        build_thermostat_status(zone="PZ1"),
    ]:
        record = evaluate_device_support(apply_device_message(record, message))

    assert record.supported is True
    assert record.profile_key == "thermostat"

    dehumidifier = _build_supported_record()

    assert dehumidifier.supported is True
    assert dehumidifier.profile_key == "dehumidifier"


def test_pending_thermostat_requests_only_thermostat_status_endpoint() -> None:
    """A pending thermostat should not request standalone dehumidifier status."""
    _, devices, _ = apply_hierarchy(build_thermostat_hierarchy(), {})
    record = evaluate_device_support(
        apply_device_message(
            devices[THERMOSTAT_DEVICE_ID],
            build_thermostat_setup(
                humidifier_installed=False,
                aircleaning_installed=False,
            ),
        )
    )

    assert record.supported is False
    assert record.unsupported_reason == "awaiting_device_settings"
    assert [
        (request.key, request.endpoint) for request in status_requests_for_record(record)
    ] == [("thermostatPZ1", "thermostat/PZ1")]


def test_thermostat_status_routes_to_zone_payload_key() -> None:
    """ThermostatStatus messages should use the matching thermostat zone key."""
    _, devices, _ = apply_hierarchy(build_thermostat_hierarchy(), {})
    record = apply_device_message(devices[THERMOSTAT_DEVICE_ID], build_thermostat_status(zone="SZ2"))

    assert "thermostatSZ2" in record.status_payloads
    assert record.status_payloads["thermostatSZ2"]["zone"] == "SZ2"


def test_thermostat_iaq_status_requests_follow_installed_equipment() -> None:
    """Installed IAQ equipment should contribute read-only status requests."""
    _, devices, _ = apply_hierarchy(build_thermostat_hierarchy(), {})
    record = devices[THERMOSTAT_DEVICE_ID]
    for message in [
        build_thermostat_setup(
            humidifier_installed=True,
            dehumidifier_installed=False,
            freshair_installed=False,
            aircleaning_installed=True,
        ),
        build_thermostat_settings(),
    ]:
        record = evaluate_device_support(apply_device_message(record, message))

    assert [
        (request.key, request.endpoint) for request in status_requests_for_record(record)
    ] == [
        ("thermostatPZ1", "thermostat/PZ1"),
        ("thermostatSZ2", "thermostat/SZ2"),
        ("thermostatSZ3", "thermostat/SZ3"),
        ("iaq_humidifier", "humidifier"),
        ("iaq_aircleaning", "aircleaning"),
    ]


def test_installed_thermostat_iaq_status_keeps_rest_refresh_pending() -> None:
    """Installed IAQ equipment should keep REST fallback pending until status is loaded."""
    _, devices, _ = apply_hierarchy(build_thermostat_hierarchy(), {})
    record = devices[THERMOSTAT_DEVICE_ID]
    for message in [
        build_thermostat_setup(
            humidifier_installed=True,
            dehumidifier_installed=False,
            freshair_installed=False,
            aircleaning_installed=True,
        ),
        build_thermostat_settings(),
        build_thermostat_status(zone="PZ1"),
        build_thermostat_status(zone="SZ2"),
        build_thermostat_status(zone="SZ3"),
    ]:
        record = evaluate_device_support(apply_device_message(record, message))

    assert record_requires_rest_refresh(record) is True

    for message in [
        build_iaq_status(message_type="HumidifierStatus"),
        build_iaq_status(message_type="AirCleaningStatus"),
    ]:
        record = evaluate_device_support(apply_device_message(record, message))

    assert record_requires_rest_refresh(record) is False


def test_rest_refresh_removes_stale_thermostat_zones_from_entity_descriptions() -> None:
    """Full REST settings should let removed thermostat zones disappear."""
    _, devices, _ = apply_hierarchy(build_thermostat_hierarchy(), {})
    record = devices[THERMOSTAT_DEVICE_ID]
    for message in [
        build_thermostat_setup(),
        build_thermostat_settings(),
        build_thermostat_status(zone="PZ1"),
        build_thermostat_status(zone="SZ2"),
        build_thermostat_status(zone="SZ3"),
        build_iaq_status(message_type="HumidifierStatus"),
        build_iaq_status(message_type="AirCleaningStatus"),
    ]:
        record = evaluate_device_support(apply_device_message(record, message))

    refreshed_settings = build_thermostat_settings()
    refreshed_settings.pop("thermostatSZ3")
    refreshed_settings["asOf"] = "2026-03-24T00:10:00.000Z"
    pz1_status = build_thermostat_status(zone="PZ1")
    pz1_status["asOf"] = "2026-03-24T00:10:00.000Z"
    sz2_status = build_thermostat_status(zone="SZ2")
    sz2_status["asOf"] = "2026-03-24T00:10:00.000Z"
    refreshed = evaluate_device_support(
        apply_rest_refresh(
            record,
            device_status=build_device_status(THERMOSTAT_DEVICE_ID, model="8920W"),
            settings=refreshed_settings,
            status_payloads={
                "thermostatPZ1": pz1_status,
                "thermostatSZ2": sz2_status,
            },
        )
    )

    profile = profiles_module.get_profile(refreshed.profile_key)
    assert profile is not None
    assert profile.entity_descriptions(refreshed).climate_keys == (
        "thermostat_pz1",
        "thermostat_sz2",
    )


def test_thermostat_owned_iaq_status_routes_without_touching_standalone_dehumidifier() -> None:
    """Thermostat-owned IAQ status should not change standalone dehumidifier routing."""
    _, thermostat_devices, _ = apply_hierarchy(build_thermostat_hierarchy(), {})
    thermostat = apply_device_message(
        thermostat_devices[THERMOSTAT_DEVICE_ID],
        build_thermostat_setup(),
    )
    thermostat = apply_device_message(
        thermostat,
        build_iaq_status(message_type="DehumidifierStatus"),
    )

    assert "iaq_dehumidifier" in thermostat.status_payloads
    assert "dehumidifier" not in thermostat.status_payloads

    _, dehumidifier_devices, _ = apply_hierarchy(build_hierarchy(), {})
    dehumidifier = apply_device_message(
        dehumidifier_devices[DEVICE_ID],
        build_dehumidifier_status(),
    )

    assert "dehumidifier" in dehumidifier.status_payloads
    assert "iaq_dehumidifier" not in dehumidifier.status_payloads
