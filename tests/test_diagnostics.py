"""Diagnostics tests for AprilAire Cloud."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aprilaire_cloud.const import DOMAIN
from custom_components.aprilaire_cloud.coordinator import AprilaireCloudDataUpdateCoordinator
from custom_components.aprilaire_cloud.data import AprilaireCloudRuntimeData
from custom_components.aprilaire_cloud.diagnostics import async_get_config_entry_diagnostics
from custom_components.aprilaire_cloud.state import DeviceWriteState

from .common import DEVICE_ID, PASSWORD, USERNAME, build_user
from .test_coordinator import FakeClient, FakeWebSocket, bootstrap_coordinator


async def test_diagnostics_include_runtime_state_and_redact_credentials(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Diagnostics should expose reducer/runtime state without leaking credentials."""
    client = FakeClient()
    client.rate_limited_until = None
    monkeypatch.setattr(
        "custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=build_user()["userId"],
        data={"username": USERNAME, "password": PASSWORD},
    )
    entry.add_to_hass(hass)

    coordinator = AprilaireCloudDataUpdateCoordinator(hass, config_entry=entry, client=client)
    await bootstrap_coordinator(coordinator)

    device = coordinator.data.devices[DEVICE_ID]
    coordinator._devices[DEVICE_ID] = replace(
        device,
        pending_device_settings={"dehumidifier": {"humiditySetpoint": 60}},
    )
    write_state = DeviceWriteState(
        pending_paths=("dehumidifier.humiditySetpoint",),
        inflight_paths=("dehumidifier.humiditySetpoint",),
    )
    write_state.inflight_expected = {"dehumidifier": {"humiditySetpoint": 60}}
    write_state.inflight_event = asyncio.Event()
    coordinator._write_states[DEVICE_ID] = write_state
    coordinator.async_set_updated_data(coordinator._build_snapshot())

    entry.runtime_data = AprilaireCloudRuntimeData(
        client=client,
        coordinator=coordinator,
        integration=None,  # type: ignore[arg-type]
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["entry"]["data"]["password"] == "**REDACTED**"
    assert diagnostics["entry"]["data"]["username"] == "**REDACTED**"
    assert diagnostics["entry"]["title"].startswith("sha256:")
    assert diagnostics["snapshot"]["user_id"].startswith("sha256:")
    assert diagnostics["snapshot"]["email"].startswith("sha256:")
    assert diagnostics["snapshot"]["locations"]["00000000000000000000000000000000"][
        "name"
    ].startswith("sha256:")
    assert diagnostics["snapshot"]["devices"][DEVICE_ID]["pending_device_settings"] == {
        "dehumidifier": {"humiditySetpoint": 60}
    }
    assert (
        diagnostics["snapshot"]["devices"][DEVICE_ID]["effective_device_settings"]["dehumidifier"][
            "humiditySetpoint"
        ]
        == 60
    )
    assert diagnostics["snapshot"]["write_states"][DEVICE_ID]["pending_paths"] == [
        "dehumidifier.humiditySetpoint"
    ]
    assert diagnostics["snapshot"]["write_states"][DEVICE_ID]["waiting_for_confirmation"] is True
