"""Data coordinator for AprilAire Cloud."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.issue_registry import IssueSeverity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    AprilaireCloudApiClient,
    AprilaireCloudApiError,
    AprilaireCloudAuthenticationError,
    AprilaireCloudCommunicationError,
    AprilaireCloudRateLimitError,
    AprilaireCloudWriteError,
)
from .const import (
    CONF_ENABLE_EXTRA_DIAGNOSTICS,
    CONF_FALLBACK_REFRESH_MINUTES,
    CONF_SAFETY_REFRESH_MINUTES,
    DEFAULT_FALLBACK_REFRESH_MINUTES,
    DEFAULT_SAFETY_REFRESH_MINUTES,
    DOMAIN,
    ISSUE_NO_SUPPORTED_DEVICES,
    ISSUE_UNSUPPORTED_DEVICES,
    LOGGER,
    MAX_PARALLEL_REST_REQUESTS,
    POST_WRITE_CONFIRM_TIMEOUT,
    UNKNOWN_DEVICE_MESSAGE_MAX_PER_DEVICE,
    UNKNOWN_DEVICE_MESSAGE_TTL_SECONDS,
    WEBSOCKET_INITIAL_SYNC_TIMEOUT,
)
from .data import AprilaireCloudConfigEntry
from .models import AprilaireSnapshot, DeviceRecord, HierarchyLocation, SocketState
from .profiles import (
    format_unsupported_reasons,
    record_has_thermostat_hint,
    record_requires_rest_refresh,
    status_requests_for_record,
)
from .state import (
    DeviceWriteState,
    apply_confirmed_device_settings,
    apply_device_message,
    apply_full_device_settings,
    apply_hierarchy,
    apply_pending_device_settings,
    apply_rest_refresh,
    clear_pending_device_settings,
    evaluate_device_support,
    format_leaf_paths,
    pending_payload_is_current,
    settings_match_payload,
)
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
            update_interval=timedelta(
                minutes=int(
                    config_entry.options.get(
                        CONF_SAFETY_REFRESH_MINUTES,
                        DEFAULT_SAFETY_REFRESH_MINUTES,
                    )
                )
            ),
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
        self._write_states: dict[str, DeviceWriteState] = {}
        self._unknown_device_messages: dict[str, list[tuple[float, dict[str, Any]]]] = {}
        self._last_rest_refresh_at: datetime | None = None
        self._last_websocket_message_at: dict[str, datetime] = {}
        self._current_refresh_mode: str = "safety"

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
        await self._async_rest_refresh_devices(self._device_ids_requiring_rest_refresh())
        self._update_support_issue()
        self._publish_snapshot()

    async def _async_update_data(self) -> AprilaireSnapshot:
        """Perform a slow safety refresh or a bounded REST fallback refresh."""
        LOGGER.debug(
            "Starting %s refresh, %d devices tracked",
            self._current_refresh_mode,
            len(self._devices),
        )
        try:
            old_location_ids = set(self._locations)
            hierarchy = await self.client.async_get_hierarchy()
            removed_ids = self._apply_hierarchy(hierarchy)
            removed_location_ids = old_location_ids - set(self._locations)
            await self._async_sync_location_websockets()

            rest_refresh_ids = self._device_ids_requiring_rest_refresh()
            refreshed_ids: set[str] = set()
            refresh_errors: dict[str, Exception] = {}
            if rest_refresh_ids:
                refreshed_ids, refresh_errors = await self._async_rest_refresh_devices(
                    rest_refresh_ids
                )
                if refresh_errors and not refreshed_ids:
                    self._raise_refresh_error(next(iter(refresh_errors.values())))

            await self._async_cleanup_removed_locations(removed_location_ids)
            await self._async_cleanup_removed_devices(removed_ids)
            self._update_support_issue()
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
        ir.async_delete_issue(
            self.hass,
            DOMAIN,
            self._unsupported_devices_issue_id,
        )
        ir.async_delete_issue(
            self.hass,
            DOMAIN,
            self._no_supported_devices_issue_id,
        )
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
        support_changed = False
        for message in messages:
            message_type = message.get("_type")
            if message_type == "RefreshEvent":
                LOGGER.debug("RefreshEvent received for location %s", location_id)
                self._schedule_refresh()
                continue

            device_id = message.get("deviceId")
            if device_id is None:
                continue
            LOGGER.debug(
                "WebSocket message %s for device %s in location %s",
                message_type,
                device_id,
                location_id,
            )
            if device_id not in self._devices:
                LOGGER.debug("Caching message for unknown device %s", device_id)
                self._cache_unknown_device_message(device_id, message)
                self._schedule_refresh()
                continue

            record = self._devices[device_id]
            updated_record = evaluate_device_support(apply_device_message(record, message))
            if updated_record == record:
                continue
            self._devices[device_id] = updated_record
            changed = True
            if (
                updated_record.supported != record.supported
                or updated_record.unsupported_reason != record.unsupported_reason
            ):
                support_changed = True
            if message_type == "DeviceSettings":
                write_state = self._write_states.get(device_id)
                if (
                    write_state is not None
                    and write_state.inflight_event is not None
                    and settings_match_payload(message, write_state.inflight_expected)
                ):
                    write_state.inflight_event.set()

        if changed:
            if support_changed:
                self._update_support_issue()
            self._last_websocket_message_at[location_id] = datetime.now(tz=UTC)
            self._publish_snapshot()

    async def async_socket_state_changed(self, state: SocketState) -> None:
        """Track websocket connection state."""
        self._socket_states[state.location_id] = state
        self._publish_snapshot()

    async def async_write_device_settings(
        self,
        device_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Write settings and wait briefly for a matching settings confirmation."""
        if device_id not in self._devices:
            return

        write_state = self._write_states.setdefault(device_id, DeviceWriteState())
        payload_paths = format_leaf_paths(payload)
        LOGGER.debug("Write started for device %s: %s", device_id, payload_paths)
        self._apply_pending_device_settings(device_id, payload)
        self._sync_write_state(device_id, confirmed_settings=None)
        pending_paths = set(write_state.pending_paths)
        pending_paths.update(payload_paths)
        write_state.pending_paths = tuple(sorted(pending_paths))
        self._publish_snapshot()

        should_raise = False
        try:
            async with write_state.lock:
                if not self._pending_payload_is_current(device_id, payload):
                    return

                inflight_event = asyncio.Event()
                write_state.inflight_paths = payload_paths
                write_state.inflight_expected = deepcopy(payload)
                write_state.inflight_event = inflight_event

                await self.client.async_patch_device_settings(device_id, payload)
                try:
                    await asyncio.wait_for(
                        inflight_event.wait(), timeout=POST_WRITE_CONFIRM_TIMEOUT
                    )
                    LOGGER.debug("Write confirmed via WebSocket for device %s", device_id)
                    return
                except TimeoutError:
                    LOGGER.debug(
                        "Write confirmation timed out for device %s, checking REST",
                        device_id,
                    )
                    settings = await self._async_refresh_device_settings(device_id)
                    self._publish_snapshot()
                    if settings_match_payload(settings, payload):
                        LOGGER.debug("Write confirmed via REST for device %s", device_id)
                        return

                    should_raise = self._pending_payload_is_current(device_id, payload)
        except (AprilaireCloudApiError, AprilaireCloudCommunicationError):
            should_raise = should_raise or self._pending_payload_is_current(device_id, payload)
            if should_raise:
                self._clear_pending_device_settings(device_id, payload)
                self._sync_write_state(device_id, confirmed_settings=None)
                self._publish_snapshot()
                raise
            return
        finally:
            write_state.inflight_paths = ()
            write_state.inflight_expected = {}
            write_state.inflight_event = None
            self._sync_write_state(device_id, confirmed_settings=None)

        if should_raise:
            self._clear_pending_device_settings(device_id, payload)
            self._sync_write_state(device_id, confirmed_settings=None)
            self._publish_snapshot()
            raise AprilaireCloudWriteError("AprilAire did not confirm updated settings")

    def _build_snapshot(self) -> AprilaireSnapshot:
        """Build an immutable snapshot for entities, dynamically flagging omitted devices."""
        # Check the global integration refresh mode state
        if self._current_refresh_mode == "fallback":
            # Fetch the collection of device IDs that are actively failing/requiring refresh cycles
            try:
                active_rest_ids = self._device_ids_requiring_rest_refresh()
                
                # Loop through the compiled devices inside the shared cache layer
                for device_id, record in list(self._devices.items()):
                    # IF the integration is running fallback routines, but this specific device ID
                    # is entirely missing from the active refresh queue, it means it is uncommunicative.
                    if active_rest_ids and device_id not in active_rest_ids:
                        if hasattr(record, "device_status") and isinstance(record.device_status, dict):
                            # Inject our unique offline indicator flag for this device container alone
                            record.device_status["_hardware_offline"] = True
            except Exception:
                pass

        return AprilaireSnapshot(
            user_id=self._user_id,
            email=self._email,
            locations=dict(self._locations),
            devices=dict(self._devices),
            socket_states=dict(self._socket_states),
        )

    def _publish_snapshot(self) -> None:
        """Publish the current snapshot to entities."""
        self.async_set_updated_data(self._build_snapshot())
        self._update_refresh_interval()

    def _apply_pending_device_settings(self, device_id: str, payload: dict[str, Any]) -> None:
        """Merge optimistic local settings into the pending override layer."""
        record = self._devices.get(device_id)
        if record is None:
            return
        self._devices[device_id] = evaluate_device_support(
            apply_pending_device_settings(record, payload)
        )

    def _apply_confirmed_device_settings(self, device_id: str, settings: dict[str, Any]) -> None:
        """Update confirmed remote settings and clear any matching optimistic overrides."""
        record = self._devices.get(device_id)
        if record is None:
            return
        self._devices[device_id] = evaluate_device_support(
            apply_confirmed_device_settings(record, settings)
        )
        self._sync_write_state(device_id, confirmed_settings=settings)

    def _apply_full_device_settings(self, device_id: str, settings: dict[str, Any]) -> None:
        """Replace confirmed remote settings from a REST settings payload."""
        record = self._devices.get(device_id)
        if record is None:
            return
        self._devices[device_id] = evaluate_device_support(
            apply_full_device_settings(record, settings)
        )
        self._sync_write_state(device_id, confirmed_settings=settings)

    def _clear_pending_device_settings(self, device_id: str, payload: dict[str, Any]) -> None:
        """Remove matching optimistic override paths from the pending layer."""
        record = self._devices.get(device_id)
        if record is None:
            return
        self._devices[device_id] = evaluate_device_support(
            clear_pending_device_settings(record, payload)
        )
        self._sync_write_state(device_id, confirmed_settings=None)

    def _pending_payload_is_current(self, device_id: str, payload: dict[str, Any]) -> bool:
        """Return whether a request still matches the latest pending local override."""
        record = self._devices.get(device_id)
        if record is None:
            return False
        return pending_payload_is_current(record, payload)

    def _apply_hierarchy(self, hierarchy: dict[str, Any]) -> set[str]:
        """Merge hierarchy data and return removed device IDs."""
        old_device_ids = set(self._devices)
        locations, devices, removed_ids = apply_hierarchy(hierarchy, self._devices)
        self._locations = locations
        self._devices = devices
        self._replay_unknown_device_messages()
        new_device_ids = set(self._devices) - old_device_ids
        if new_device_ids:
            LOGGER.debug("Devices added to hierarchy: %s", new_device_ids)
        if removed_ids:
            LOGGER.debug("Devices removed from hierarchy: %s", removed_ids)
        for device_id in removed_ids:
            self._write_states.pop(device_id, None)
            self._unknown_device_messages.pop(device_id, None)
        return removed_ids

    def _device_ids_requiring_rest_refresh(self) -> set[str]:
        """Return device IDs that still need REST fallback refreshes."""
        if not self._devices:
            return set()

        unhealthy_locations = {
            location_id
            for location_id in self._locations
            if (
                location_id not in self._socket_states
                or not self._socket_states[location_id].connected
                or not self._socket_states[location_id].initial_sync_complete
            )
        }
        return {
            device_id
            for device_id, record in self._devices.items()
            if record_requires_rest_refresh(
                record,
                location_unhealthy=record.hierarchy.location_id in unhealthy_locations,
            )
        }

    def _update_refresh_interval(self) -> None:
        """Switch between safety refresh and bounded fallback refresh."""
        needs_fallback = bool(self._device_ids_requiring_rest_refresh())
        new_mode = "fallback" if needs_fallback else "safety"
        if new_mode != self._current_refresh_mode:
            LOGGER.info(
                "Refresh mode changed from %s to %s",
                self._current_refresh_mode,
                new_mode,
            )
            self._current_refresh_mode = new_mode
        self.update_interval = (
            self._fallback_refresh_interval if needs_fallback else self._safety_refresh_interval
        )

    async def _async_sync_location_websockets(self, *, wait_for_ready: bool = False) -> None:
        """Ensure websocket managers exist for every known location."""
        existing_locations = set(self._websockets)
        wanted_locations = set(self._locations)

        for location_id in existing_locations - wanted_locations:
            await self._websockets.pop(location_id).async_stop()
            self._socket_states.pop(location_id, None)
            self._last_websocket_message_at.pop(location_id, None)

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

    async def _async_rest_refresh_devices(
        self,
        device_ids: Iterable[str],
    ) -> tuple[set[str], dict[str, Exception]]:
        """Refresh device state via REST."""
        ids = list(device_ids)
        if not ids:
            return set(), {}
        LOGGER.debug("REST refresh starting for %d device(s): %s", len(ids), ids)

        semaphore = asyncio.Semaphore(MAX_PARALLEL_REST_REQUESTS)

        async def _refresh_device(
            device_id: str,
        ) -> tuple[
            str,
            dict[str, Any],
            dict[str, dict[str, Any]],
            dict[str, Any] | Exception,
        ]:
            async with semaphore:
                try:
                    status_requests = status_requests_for_record(self._devices[device_id])
                    status, settings, *profile_statuses = await asyncio.gather(
                        self.client.async_get_device_status(device_id),
                        self.client.async_get_device_settings(device_id),
                        *(
                            self.client.async_get_status(device_id, request.endpoint)
                            for request in status_requests
                        ),
                    )
                except (
                    AprilaireCloudApiError,
                    AprilaireCloudAuthenticationError,
                    AprilaireCloudCommunicationError,
                    AprilaireCloudRateLimitError,
                ) as err:
                    return device_id, {}, {}, err
                return (
                    device_id,
                    status,
                    {
                        request.key: payload
                        for request, payload in zip(
                            status_requests, profile_statuses, strict=True
                        )
                    },
                    settings,
                )

        # ... [Keep your existing asyncio.gather and results processing up to line 38] ...

        results = await asyncio.gather(*(_refresh_device(device_id) for device_id in ids))
        refreshed_ids: set[str] = set()
        refresh_errors: dict[str, Exception] = {}

        for device_id, status, status_payloads, settings in results:
            if device_id not in self._devices:
                continue
                
            # === THE CORRECTION BLOCK ===
            # If the REST refresh threw an exception, it means the cloud server can no longer 
            # talk to the physical device. We explicitly mutate its local memory record 
            # to break the mirroring loop and alert Home Assistant.
            if isinstance(settings, Exception):
                LOGGER.warning("REST refresh failed for device %s: %s", device_id, settings)
                refresh_errors[device_id] = settings
                
                # Fetch the current record for this device
                record = self._devices[device_id]
                if record is not None:
                    # Explicitly break the data cache for this device container alone
                    # We inject a native python None state or flag to drop its availability
                    if hasattr(record, "device_status") and isinstance(record.device_status, dict):
                        # Force a unique, local payload key change that the entities can read natively
                        record.device_status["_hardware_offline"] = True
                continue
            # ============================

            self._devices[device_id] = evaluate_device_support(
                apply_rest_refresh(
                    self._devices[device_id],
                    device_status=status,
                    settings=settings,
                    status_payloads=status_payloads,
                )
            )
            self._sync_write_state(device_id, confirmed_settings=settings)
            refreshed_ids.add(device_id)
            
        if refreshed_ids:
            self._last_rest_refresh_at = datetime.now(tz=UTC)
        return refreshed_ids, refresh_errors

    async def _async_refresh_device_settings(self, device_id: str) -> dict[str, Any]:
        """Refresh only the writable settings for one device."""
        settings = await self.client.async_get_device_settings(device_id)
        record = self._devices.get(device_id)
        if record is not None and record_has_thermostat_hint(record):
            self._apply_full_device_settings(device_id, settings)
        else:
            self._apply_confirmed_device_settings(device_id, settings)
        return settings

    async def _async_cleanup_removed_devices(self, removed_ids: set[str]) -> None:
        """Remove stale entity registry entries for devices no longer in the hierarchy."""
        if not removed_ids:
            return
        LOGGER.debug("Cleaning up removed devices: %s", removed_ids)
        device_registry = dr.async_get(self.hass)
        entity_registry = er.async_get(self.hass)
        stale_registry_device_ids = {
            device.id
            for device in dr.async_entries_for_config_entry(
                device_registry, self.config_entry.entry_id
            )
            if any(
                identifier[0] == DOMAIN and identifier[1] in removed_ids
                for identifier in device.identifiers
            )
        }

        for registry_entry in er.async_entries_for_config_entry(
            entity_registry,
            self.config_entry.entry_id,
        ):
            if registry_entry.device_id in stale_registry_device_ids or (
                registry_entry.unique_id
                and any(
                    registry_entry.unique_id.startswith(f"{device_id}_")
                    for device_id in removed_ids
                )
            ):
                entity_registry.async_remove(registry_entry.entity_id)

        for registry_device_id in stale_registry_device_ids:
            device_registry.async_remove_device(registry_device_id)

    async def _async_cleanup_removed_locations(self, removed_location_ids: set[str]) -> None:
        """Remove stale entity and device registry entries for removed locations."""
        if not removed_location_ids:
            return
        LOGGER.debug("Cleaning up removed locations: %s", removed_location_ids)
        device_registry = dr.async_get(self.hass)
        entity_registry = er.async_get(self.hass)
        stale_registry_device_ids = {
            device.id
            for device in dr.async_entries_for_config_entry(
                device_registry, self.config_entry.entry_id
            )
            if any(
                identifier[0] == DOMAIN and identifier[1] == f"location_{location_id}"
                for identifier in device.identifiers
                for location_id in removed_location_ids
            )
        }

        for registry_entry in er.async_entries_for_config_entry(
            entity_registry,
            self.config_entry.entry_id,
        ):
            if registry_entry.device_id in stale_registry_device_ids or (
                registry_entry.unique_id
                and any(
                    registry_entry.unique_id == f"{location_id}_websocket_connection"
                    for location_id in removed_location_ids
                )
            ):
                entity_registry.async_remove(registry_entry.entity_id)

        for registry_device_id in stale_registry_device_ids:
            device_registry.async_remove_device(registry_device_id)

    def _schedule_refresh(self) -> None:
        """Debounce refresh-event triggered hierarchy reloads."""
        if self._refresh_event_task is not None and not self._refresh_event_task.done():
            return

        async def _refresh() -> None:
            await self.async_request_refresh()

        self._refresh_event_task = self.hass.async_create_task(_refresh())

    @property
    def _safety_refresh_interval(self) -> timedelta:
        """Return the configured safety refresh interval."""
        minutes = int(
            self.config_entry.options.get(
                CONF_SAFETY_REFRESH_MINUTES,
                DEFAULT_SAFETY_REFRESH_MINUTES,
            )
        )
        return timedelta(minutes=minutes)

    @property
    def _fallback_refresh_interval(self) -> timedelta:
        """Return the configured fallback refresh interval."""
        minutes = int(
            self.config_entry.options.get(
                CONF_FALLBACK_REFRESH_MINUTES,
                DEFAULT_FALLBACK_REFRESH_MINUTES,
            )
        )
        return timedelta(minutes=minutes)

    @property
    def extra_diagnostics_enabled(self) -> bool:
        """Return whether diagnostic state should be exposed by default."""
        return bool(self.config_entry.options.get(CONF_ENABLE_EXTRA_DIAGNOSTICS, False))

    @property
    def write_state_summaries(self) -> dict[str, dict]:
        """Return a diagnostics-friendly summary of write states."""
        return {
            device_id: state.summary
            for device_id, state in self._write_states.items()
        }

    @property
    def unknown_device_message_counts(self) -> dict[str, int]:
        """Return the count of cached messages per unknown device."""
        return {
            device_id: len(messages)
            for device_id, messages in self._unknown_device_messages.items()
        }

    @property
    def last_rest_refresh_at(self) -> datetime | None:
        """Return when the last successful REST refresh completed."""
        return self._last_rest_refresh_at

    @property
    def last_websocket_message_at(self) -> dict[str, datetime]:
        """Return per-location timestamps of the last websocket message."""
        return dict(self._last_websocket_message_at)

    @property
    def current_refresh_mode(self) -> str:
        """Return the current refresh mode (safety or fallback)."""
        return self._current_refresh_mode

    @property
    def current_refresh_interval_seconds(self) -> float:
        """Return the current refresh interval in seconds."""
        if self.update_interval is None:
            return 0.0
        return self.update_interval.total_seconds()

    @property
    def _unsupported_devices_issue_id(self) -> str:
        """Return the issue id used for unsupported devices."""
        return f"{ISSUE_UNSUPPORTED_DEVICES}_{self.config_entry.entry_id}"

    @property
    def _no_supported_devices_issue_id(self) -> str:
        """Return the issue id used when no supported devices are available."""
        return f"{ISSUE_NO_SUPPORTED_DEVICES}_{self.config_entry.entry_id}"

    def _sync_write_state(
        self,
        device_id: str,
        *,
        confirmed_settings: dict[str, Any] | None,
    ) -> None:
        """Sync write-state metadata from the current device record."""
        record = self._devices.get(device_id)
        if record is None:
            self._write_states.pop(device_id, None)
            return

        write_state = self._write_states.setdefault(device_id, DeviceWriteState())
        write_state.pending_paths = format_leaf_paths(record.pending_device_settings)
        if confirmed_settings is not None:
            write_state.last_confirmed_settings = deepcopy(confirmed_settings)
        if (
            not write_state.pending_paths
            and not write_state.inflight_paths
            and write_state.inflight_event is None
        ):
            self._write_states.pop(device_id, None)

    def _cache_unknown_device_message(self, device_id: str, message: dict[str, Any]) -> None:
        """Cache websocket messages for devices that are not in the hierarchy yet."""
        self._prune_unknown_device_messages()
        cached = self._unknown_device_messages.setdefault(device_id, [])
        cached.append((monotonic(), deepcopy(message)))
        if len(cached) > UNKNOWN_DEVICE_MESSAGE_MAX_PER_DEVICE:
            del cached[:-UNKNOWN_DEVICE_MESSAGE_MAX_PER_DEVICE]

    def _prune_unknown_device_messages(self) -> None:
        """Drop expired cached websocket messages."""
        cutoff = monotonic() - UNKNOWN_DEVICE_MESSAGE_TTL_SECONDS
        expired_device_ids = []
        for device_id, messages in self._unknown_device_messages.items():
            self._unknown_device_messages[device_id] = [
                (timestamp, message)
                for timestamp, message in messages
                if timestamp >= cutoff
            ]
            if not self._unknown_device_messages[device_id]:
                expired_device_ids.append(device_id)

        for device_id in expired_device_ids:
            self._unknown_device_messages.pop(device_id, None)

    def _replay_unknown_device_messages(self) -> None:
        """Replay cached websocket messages for newly discovered devices."""
        self._prune_unknown_device_messages()
        for device_id in tuple(self._unknown_device_messages):
            if device_id not in self._devices:
                continue
            record = self._devices[device_id]
            for _, message in self._unknown_device_messages.pop(device_id):
                record = evaluate_device_support(apply_device_message(record, message))
            self._devices[device_id] = record

    def _raise_refresh_error(self, err: Exception) -> None:
        """Raise the correct coordinator refresh error for a REST failure."""
        if isinstance(err, AprilaireCloudAuthenticationError):
            raise ConfigEntryAuthFailed from err
        if isinstance(err, AprilaireCloudRateLimitError):
            raise UpdateFailed(
                "AprilAire REST API rate limited",
                retry_after=err.retry_after,
            ) from err
        if isinstance(err, (AprilaireCloudCommunicationError, AprilaireCloudApiError)):
            raise UpdateFailed(f"Unable to refresh AprilAire data: {err}") from err
        raise UpdateFailed(f"Unable to refresh AprilAire data: {err}") from err

    def _update_support_issue(self) -> None:
        """Create or clear repair issues for unsupported device combinations."""
        supported = [device for device in self._devices.values() if device.supported]
        unsupported = [device for device in self._devices.values() if not device.supported]
        reason_counts: dict[str, int] = {}
        for device in unsupported:
            if device.unsupported_reason is None:
                continue
            reason_counts[device.unsupported_reason] = (
                reason_counts.get(device.unsupported_reason, 0) + 1
            )

        if unsupported and not supported:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._no_supported_devices_issue_id,
                is_fixable=False,
                is_persistent=False,
                severity=IssueSeverity.WARNING,
                translation_key=ISSUE_NO_SUPPORTED_DEVICES,
                translation_placeholders={
                    "unsupported_count": str(len(unsupported)),
                    "reason_summary": format_unsupported_reasons(reason_counts),
                },
            )
            ir.async_delete_issue(
                self.hass,
                DOMAIN,
                self._unsupported_devices_issue_id,
            )
            return

        if supported and unsupported:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._unsupported_devices_issue_id,
                is_fixable=False,
                is_persistent=False,
                severity=IssueSeverity.WARNING,
                translation_key=ISSUE_UNSUPPORTED_DEVICES,
                translation_placeholders={
                    "supported_count": str(len(supported)),
                    "unsupported_count": str(len(unsupported)),
                    "reason_summary": format_unsupported_reasons(reason_counts),
                },
            )
            ir.async_delete_issue(
                self.hass,
                DOMAIN,
                self._no_supported_devices_issue_id,
            )
            return

        ir.async_delete_issue(
            self.hass,
            DOMAIN,
            self._unsupported_devices_issue_id,
        )
        ir.async_delete_issue(
            self.hass,
            DOMAIN,
            self._no_supported_devices_issue_id,
        )
