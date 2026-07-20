"""Tests for deferred dynamic-entity synchronization."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.aprilaire_cloud.entity import (
    DynamicEntityDescriptor,
    setup_dynamic_platform_entities,
)
from custom_components.aprilaire_cloud.models import (
    AprilaireSnapshot,
    DeviceRecord,
    HierarchyDevice,
)


def test_noop_sync_does_not_construct_throwaway_entities() -> None:
    """An unchanged descriptor set constructs its entity exactly once."""
    device = DeviceRecord(
        hierarchy=HierarchyDevice(
            device_id="device-001",
            location_id="location-001",
            location_name="Synthetic Home",
        ),
        supported=True,
    )
    listeners = []
    added = []
    create_count = 0

    class FakeEntity:
        unique_id = "device-001_sensor"
        hass = None

    def _create():
        nonlocal create_count
        create_count += 1
        return FakeEntity()

    coordinator = SimpleNamespace(
        data=AprilaireSnapshot(
            user_id="user-001",
            email="user@example.com",
            devices={"device-001": device},
        ),
        hass=SimpleNamespace(async_create_task=lambda task: None),
        async_add_listener=lambda callback: (
            listeners.append(callback) or (lambda: None)
        ),
    )
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(coordinator=coordinator),
        async_on_unload=lambda unsubscribe: None,
    )

    setup_dynamic_platform_entities(
        entry,
        lambda entities: added.extend(entities),
        lambda device_id, record: (
            DynamicEntityDescriptor(
                unique_id=f"{device_id}_sensor",
                factory=_create,
            ),
        ),
    )
    listeners[0]()
    listeners[0]()

    assert create_count == 1
    assert len(added) == 1
