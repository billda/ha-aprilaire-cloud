"""Data coordinator for AprilAire Cloud."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import replace
from datetime import timedelta
from typing import Any

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.exceptions import ConfigEntryAuthFailed

from .api import (
    AprilaireCloudApiClient,
    AprilaireCloudApiError,
    AprilaireCloudAuthenticationError,
    AprilaireCloudCommunicationError,
    AprilaireCloudRateLimitError,
)
from .const import (
    DEFAULT_FALLBACK_REFRESH_INTERVAL,
    DEFAULT_SAFETY_REFRESH_INTERVAL,
    DOMAIN,
    LOGGER,
    MAX_PARALLEL_REST_REQUESTS,
    POST_WRITE_CONFIRM_TIMEOUT,
    SUPPORTED_CONTROL_TYPE,
    SUPPORTED_REPORTING_TYPE,
    SUPPORTED_SCALE,
    WEBSOCKET_INITIAL_SYNC_TIMEOUT,
)
from .data import AprilaireCloudConfigEntry
from .models import AprilaireSnapshot, DeviceRecord, HierarchyDevice, HierarchyLocation, SocketState, empty_snapshot
from .websocket import AprilaireLocationWebSocket


class AprilaireCloudDataUpdateCoordinator(DataUpdateCoordinator[AprilaireSnapshot]):
    """Coordinate push and fallback REST updates."""

    config_entry: AprilaireCloudConfigEntry

    def __init__(
        self,
        hass,
        *,
        config_entry: AprilaireCloudConfigEntry,
        client: AprilaireCloudApiClient,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=DEFAULT_SAFETY_REFRESH_INTERVAL,
            always_update=False,
        )
        self.client = client
        self._user_id = ""
        self._email = client.username
        self._locations: dict[str, HierarchyLocation] = {}
        self._devices: dict[str, DeviceRecord] = {}
        self._socket_states: dict[str, SocketState] = {}
        self._websockets: dict[str, AprilaireLocationWebSocket] = {}
        self._refresh_event_task: asyncio.Task[None] | None = None
        self._write_waiters: dict[str, set[asyncio.Event]] = defaultdict(set)

    async def _async_setup(self) -> None:
        """Perform one-time startup work."""
        try:
            user = await self.client.async_get_user()
            hierarchy = await self.client.async_get_hierarchy()
        except AprilaireCloudAuthenticationError as err:
            raise ConfigEntryAuthFailed from err
        except AprilaireCloudRateLimitError as err:
            raise UpdateFailed(
                "AprilAire REST API rate limited during setup",
                retry_after=err.retry_after,
            ) from err
        except (AprilaireCloudApiError, AprilaireCloudCommunicationError) as err:
            raise UpdateFailed(f"Unable to initialize AprilAire integration: {err}") from err

        self._user_id = str(user["userId"])
        self._email = user.get("email", self.client.username)
        self._apply_hierarchy(hierarchy)
        self.data = self._build_snapshot()

        await self._async_sync_location_websockets(wait_for_ready=True)
        await self._async_rest_refresh_devices(
            [
                device_id
                for device_id, record in self._devices.items()
                if not record.device_settings or not record.dehumidifier_status
            ]
        )
        self.async_set_updated_data(self._build_snapshot())
        self._update_refresh_interval()

    async def _async_update_data(self) -> AprilaireSnapshot:
        """Perform a slow safety refresh or a bounded REST fallback refresh."""
        try:
            hierarchy = await self.client.async_get_hierarchy()
            removed_ids = self._apply_hierarchy(hierarchy)
            await self._async_sync_location_websockets()

            should_rest_refresh = self._needs_rest_fallback()
            if should_rest_refresh:
                await self._async_rest_refresh_devices(self._devices)

            await self._async_cleanup_removed_devices(removed_ids)
        except AprilaireCloudAuthenticationError as err:
            raise ConfigEntryAuthFailed from err
        except AprilaireCloudRateLimitError as err:
            raise UpdateFailed(
                "AprilAire REST API rate limited",
                retry_after=err.retry_after,
            ) from err
        except (AprilaireCloudCommunicationError, AprilaireCloudApiError) as err:
            raise UpdateFailed(f"Unable to refresh AprilAire data: {err}") from err

        snapshot = self._build_snapshot()
        self._update_refresh_interval()
        return snapshot

    async def async_shutdown(self) -> None:
        """Tear down runtime resources."""
        if self._refresh_event_task is not None:
            self._refresh_event_task.cancel()
        await asyncio.gather(
            *(manager.async_stop() for manager in self._websockets.values()),
            return_exceptions=True,
        )
        self._websockets.clear()

    async def async_process_messages(
        self,
        location_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        """Merge websocket messages into the live snapshot."""
        changed = False
        for message in messages:
            message_type = message.get("_type")
            if message_type == "RefreshEvent":
                self._schedule_refresh()
                continue

            device_id = message.get("deviceId")
            if device_id is None or device_id not in self._devices:
                continue

            record = self._devices[device_id]
            if message_type == "DehumidifierStatus":
                record = replace(record, dehumidifier_status=message)
            elif message_type == "DeviceSettings":
                record = replace(record, device_settings=message)
            elif message_type == "DeviceSetup":
                record = replace(record, device_setup=message)
            elif message_type == "DeviceStatus":
                record = replace(record, device_status=message)
            elif message_type == "SensorHubStatus":
                record = replace(record, sensor_hub_status=message)
            else:
                continue

            updated = self._evaluate_device_support(record)
            self._devices[device_id] = updated
            changed = True
            if message_type in {"DehumidifierStatus", "DeviceSettings"}:
                self._notify_write_waiters(device_id)

        if changed:
            self.async_set_updated_data(self._build_snapshot())
            self._update_refresh_interval()

    async def async_socket_state_changed(self, state: SocketState) -> None:
        """Track websocket connection state."""
        self._socket_states[state.location_id] = state
        self.async_set_updated_data(self._build_snapshot())
        self._update_refresh_interval()

    async def async_set_mode(self, device_id: str, enabled: bool) -> None:
        """Set the operating mode for a device."""
        await self.async_write_device_settings(
            device_id,
            {"dehumidifier": {"mode": "on" if enabled else "off"}},
        )

    async def async_set_target_humidity(self, device_id: str, humidity: int) -> None:
        """Set the target humidity for a device."""
        await self.async_write_device_settings(
            device_id,
            {"dehumidifier": {"humiditySetpoint": humidity}},
        )

    async def async_set_alert_limit(self, device_id: str, key: str, value: int) -> None:
        """Set an alert limit."""
        await self.async_write_device_settings(
            device_id,
            {"dehumidifier": {"alertLimits": {key: value}}},
        )

    async def async_write_device_settings(
        self,
        device_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Write settings and wait briefly for websocket confirmation."""
        waiter = asyncio.Event()
        self._write_waiters[device_id].add(waiter)
        try:
            await self.client.async_patch_device_settings(device_id, payload)
            try:
                await asyncio.wait_for(waiter.wait(), timeout=POST_WRITE_CONFIRM_TIMEOUT)
            except TimeoutError:
                await self._async_rest_refresh_devices([device_id])
                self.async_set_updated_data(self._build_snapshot())
        finally:
            self._write_waiters[device_id].discard(waiter)

    def _notify_write_waiters(self, device_id: str) -> None:
        """Release any callers waiting on a websocket confirmation."""
        for waiter in tuple(self._write_waiters[device_id]):
            waiter.set()

    def _build_snapshot(self) -> AprilaireSnapshot:
        """Build an immutable snapshot for entities."""
        return AprilaireSnapshot(
            user_id=self._user_id,
            email=self._email,
            locations=dict(self._locations),
            devices=dict(self._devices),
            socket_states=dict(self._socket_states),
        )

    def _evaluate_device_support(self, record: DeviceRecord) -> DeviceRecord:
        """Determine whether a device should be exposed to Home Assistant."""
        setup_type = record.device_setup.get("type")
        if setup_type != SUPPORTED_REPORTING_TYPE:
            return replace(
                record,
                supported=False,
                unsupported_reason="unsupported_equipment_type",
            )

        dehumidifier_setup = record.device_setup.get("dehumidifier", {})
        dehumidifier_settings = record.device_settings.get("dehumidifier", {})

        if not dehumidifier_setup:
            return replace(record, supported=False, unsupported_reason="awaiting_device_setup")

        if dehumidifier_setup.get("controlType") != SUPPORTED_CONTROL_TYPE:
            return replace(record, supported=False, unsupported_reason="unsupported_control_type")

        if dehumidifier_setup.get("scale") != SUPPORTED_SCALE:
            return replace(record, supported=False, unsupported_reason="unsupported_scale")

        if "humiditySetpoint" not in dehumidifier_settings:
            return replace(record, supported=False, unsupported_reason="missing_humidity_setpoint")

        if "drynessSetpoint" in dehumidifier_settings:
            return replace(record, supported=False, unsupported_reason="dryness_setpoint_unsupported")

        return replace(record, supported=True, unsupported_reason=None)

    def _apply_hierarchy(self, hierarchy: dict[str, Any]) -> set[str]:
        """Merge hierarchy data and return removed device IDs."""
        previous_device_ids = set(self._devices)
        locations: dict[str, HierarchyLocation] = {}
        devices: dict[str, DeviceRecord] = {}

        for location in hierarchy.get("locations", []):
            location_id = location["locationId"]
            locations[location_id] = HierarchyLocation(
                location_id=location_id,
                name=location.get("name", location_id),
                time_zone=location.get("timeZone"),
            )
            for room in location.get("rooms", []):
                for device in room.get("devices", []):
                    device_id = device["deviceId"]
                    hierarchy_device = HierarchyDevice(
                        device_id=device_id,
                        location_id=location_id,
                        location_name=location.get("name", location_id),
                        room_name=room.get("name"),
                        access=device.get("access"),
                        zone=device.get("zone"),
                    )
                    existing = self._devices.get(device_id)
                    if existing is None:
                        devices[device_id] = DeviceRecord(hierarchy=hierarchy_device)
                    else:
                        devices[device_id] = replace(existing, hierarchy=hierarchy_device)

        self._locations = locations
        self._devices = {
            device_id: self._evaluate_device_support(record)
            for device_id, record in devices.items()
        }
        return previous_device_ids - set(self._devices)

    def _needs_rest_fallback(self) -> bool:
        """Return whether websocket health requires REST fallback."""
        if not self._devices:
            return False
        if not self._socket_states:
            return True
        if not all(state.connected and state.initial_sync_complete for state in self._socket_states.values()):
            return True
        return any(
            not record.device_settings or not record.dehumidifier_status
            for record in self._devices.values()
        )

    def _update_refresh_interval(self) -> None:
        """Switch between safety refresh and bounded fallback refresh."""
        self.update_interval = (
            DEFAULT_FALLBACK_REFRESH_INTERVAL
            if self._needs_rest_fallback()
            else DEFAULT_SAFETY_REFRESH_INTERVAL
        )

    async def _async_sync_location_websockets(self, *, wait_for_ready: bool = False) -> None:
        """Ensure websocket managers exist for every known location."""
        existing_locations = set(self._websockets)
        wanted_locations = set(self._locations)

        for location_id in existing_locations - wanted_locations:
            await self._websockets.pop(location_id).async_stop()
            self._socket_states.pop(location_id, None)

        new_managers: list[AprilaireLocationWebSocket] = []
        for location_id in wanted_locations - existing_locations:
            manager = AprilaireLocationWebSocket(
                client=self.client,
                session=self.client.session,
                location_id=location_id,
                message_callback=self.async_process_messages,
                state_callback=self.async_socket_state_changed,
            )
            self._websockets[location_id] = manager
            new_managers.append(manager)

        await asyncio.gather(*(manager.async_start() for manager in new_managers))
        if wait_for_ready and new_managers:
            await asyncio.gather(
                *(
                    manager.async_wait_for_initial_sync(WEBSOCKET_INITIAL_SYNC_TIMEOUT)
                    for manager in new_managers
                )
            )

    async def _async_rest_refresh_devices(self, device_ids: Iterable[str]) -> None:
        """Refresh device state via REST."""
        ids = list(device_ids)
        if not ids:
            return

        semaphore = asyncio.Semaphore(MAX_PARALLEL_REST_REQUESTS)

        async def _refresh_device(device_id: str) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]:
            async with semaphore:
                status, dehumidifier_status, settings = await asyncio.gather(
                    self.client.async_get_device_status(device_id),
                    self.client.async_get_dehumidifier_status(device_id),
                    self.client.async_get_device_settings(device_id),
                )
                return device_id, status, dehumidifier_status, settings

        results = await asyncio.gather(*(_refresh_device(device_id) for device_id in ids))
        for device_id, status, dehumidifier_status, settings in results:
            if device_id not in self._devices:
                continue
            record = self._devices[device_id]
            updated = replace(
                record,
                device_status=status,
                dehumidifier_status=dehumidifier_status,
                device_settings=settings,
            )
            self._devices[device_id] = self._evaluate_device_support(updated)

    async def _async_cleanup_removed_devices(self, removed_ids: set[str]) -> None:
        """Remove stale entity registry entries for devices no longer in the hierarchy."""
        if not removed_ids:
            return
        entity_registry = er.async_get(self.hass)
        for entry in er.async_entries_for_config_entry(entity_registry, self.config_entry.entry_id):
            if entry.unique_id and any(entry.unique_id.startswith(f"{device_id}_") for device_id in removed_ids):
                entity_registry.async_remove(entry.entity_id)

    def _schedule_refresh(self) -> None:
        """Debounce refresh-event triggered hierarchy reloads."""
        if self._refresh_event_task is not None and not self._refresh_event_task.done():
            return

        async def _refresh() -> None:
            await self.async_request_refresh()

        self._refresh_event_task = self.hass.async_create_task(_refresh())
