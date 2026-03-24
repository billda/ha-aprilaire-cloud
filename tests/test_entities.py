"""Entity behavior tests for AprilAire Cloud."""

from __future__ import annotations

from homeassistant.components.humidifier import HumidifierAction
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aprilaire_cloud.const import DOMAIN
from custom_components.aprilaire_cloud.coordinator import AprilaireCloudDataUpdateCoordinator
from custom_components.aprilaire_cloud.humidifier import AprilaireCloudHumidifierEntity
from custom_components.aprilaire_cloud.sensor import AprilaireStaticSensorEntity, STATIC_SENSORS

from .common import PASSWORD, USERNAME, build_user
from .test_coordinator import FakeClient, FakeWebSocket, bootstrap_coordinator


async def test_humidifier_action_and_sensor_values(
    hass,
    enable_custom_integrations,
    monkeypatch,
) -> None:
    """Entities should map coordinator data to HA state correctly."""
    client = FakeClient()
    monkeypatch.setattr("custom_components.aprilaire_cloud.coordinator.AprilaireLocationWebSocket", FakeWebSocket)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=build_user()["userId"],
        data={"username": USERNAME, "password": PASSWORD},
    )
    entry.add_to_hass(hass)

    coordinator = AprilaireCloudDataUpdateCoordinator(hass, config_entry=entry, client=client)
    await bootstrap_coordinator(coordinator)

    humidifier = AprilaireCloudHumidifierEntity(coordinator, coordinator.data.supported_device_ids[0])
    current_humidity_sensor = AprilaireStaticSensorEntity(
        coordinator,
        coordinator.data.supported_device_ids[0],
        STATIC_SENSORS[0],
    )

    assert humidifier.is_on is True
    assert humidifier.target_humidity == 52
    assert humidifier.action is HumidifierAction.IDLE
    assert current_humidity_sensor.native_value == 49
