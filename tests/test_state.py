"""Pure reducer tests for AprilAire Cloud state helpers."""

from __future__ import annotations

from custom_components.aprilaire_cloud.models import DeviceRecord
from custom_components.aprilaire_cloud.state import (
    apply_confirmed_device_settings,
    apply_device_message,
    apply_hierarchy,
    apply_pending_device_settings,
    apply_rest_refresh,
    evaluate_device_support,
)

from .common import (
    DEVICE_ID,
    SECOND_DEVICE_ID,
    build_dehumidifier_status,
    build_device_settings,
    build_hierarchy,
    build_initial_messages,
)


def _build_supported_record() -> DeviceRecord:
    """Create a fully supported device record."""
    _, devices, _ = apply_hierarchy(build_hierarchy(), {})
    record = devices[DEVICE_ID]
    for message in build_initial_messages():
        record = evaluate_device_support(apply_device_message(record, message))
    return record


def test_apply_hierarchy_tracks_removed_devices() -> None:
    """Hierarchy refreshes should preserve surviving devices and report removals."""
    locations, devices, removed_ids = apply_hierarchy(
        build_hierarchy(include_second_device=True), {}
    )

    assert set(locations) == {"bcf1939c-1111-2222-3333-a80ac86d"}
    assert set(devices) == {DEVICE_ID, SECOND_DEVICE_ID}
    assert removed_ids == set()

    _, updated_devices, removed_ids = apply_hierarchy(build_hierarchy(), devices)

    assert set(updated_devices) == {DEVICE_ID}
    assert removed_ids == {SECOND_DEVICE_ID}


def test_evaluate_device_support_rejects_dryness_setpoint_devices() -> None:
    """Devices exposing drynessSetpoint should remain unsupported."""
    record = _build_supported_record()
    settings = build_device_settings()
    settings["dehumidifier"]["drynessSetpoint"] = 5

    updated = evaluate_device_support(apply_confirmed_device_settings(record, settings))

    assert updated.supported is False
    assert updated.unsupported_reason == "dryness_setpoint_unsupported"


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

    refreshed = evaluate_device_support(
        apply_rest_refresh(
            record,
            device_status=build_initial_messages()[3],
            dehumidifier_status=build_dehumidifier_status(humidity=51),
            settings=build_device_settings(humidity=58),
        )
    )

    assert refreshed.pending_device_settings == {}
    assert refreshed.device_settings["dehumidifier"]["humiditySetpoint"] == 58
    assert refreshed.dehumidifier_status["humSensors"][0]["reading"] == 51
