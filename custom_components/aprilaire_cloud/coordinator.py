"""Data coordinator for AprilAire Cloud."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Iterable
from contextlib import suppress
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from time import monotonic
from typing import Any

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.issue_registry import IssueSeverity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

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
    POST_WRITE_RECONCILIATION_ATTEMPTS,
    POST_WRITE_RECONCILIATION_DELAY_SECONDS,
    UNKNOWN_DEVICE_MESSAGE_MAX_PER_DEVICE,
    UNKNOWN_DEVICE_MESSAGE_TTL_SECONDS,
    WEBSOCKET_INITIAL_SYNC_TIMEOUT,
)
from .data import AprilaireCloudConfigEntry
from .models import (
    AprilaireSnapshot,
    DeviceRecord,
    HierarchyLocation,
    SocketState,
    StateSource,
)
from .profiles import (
    DeviceCommand,
    EncodedCommand,
    format_unsupported_reasons,
    get_profile,
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
    apply_rest_device_status,
    apply_status_payload,
    clear_pending_device_settings,
    evaluate_device_support,
    format_leaf_paths,
    pending_payload_is_current,
)
from .vendor import (
    AprilaireCloudApiClient,
    AprilaireCloudApiError,
    AprilaireCloudAuthenticationError,
    AprilaireCloudAuthenticationProtocolError,
    AprilaireCloudAuthenticationTransientError,
    AprilaireCloudCommunicationError,
    AprilaireCloudInvalidCredentialsError,
    AprilaireCloudRateLimitError,
    AprilaireCloudWriteError,
)
from .vendor.websocket import AprilaireLocationWebSocket


class RestFailureKind(StrEnum):
    """Sanitized REST endpoint failure categories."""

    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    TRANSPORT = "transport"
    UNSUPPORTED = "unsupported"
    HTTP = "http"


def _classify_rest_failure(error: Exception) -> RestFailureKind:
    """Classify a REST failure without retaining its message."""
    if isinstance(error, AprilaireCloudAuthenticationError):
        return RestFailureKind.AUTH
    if isinstance(error, AprilaireCloudRateLimitError):
        return RestFailureKind.RATE_LIMIT
    if isinstance(error, AprilaireCloudCommunicationError):
        return RestFailureKind.TRANSPORT
    if (
        isinstance(error, AprilaireCloudApiError)
        and error.context is not None
        and error.context.status == 404
    ):
        return RestFailureKind.UNSUPPORTED
    return RestFailureKind.HTTP


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
        self._optional_404_fingerprints: dict[tuple[str, str], str] = {}
        self._optional_endpoint_failures: dict[tuple[str, str], RestFailureKind] = {}
        self._rest_freshness_window = timedelta(
            minutes=max(
                int(
                    config_entry.options.get(
                        CONF_SAFETY_REFRESH_MINUTES,
                        DEFAULT_SAFETY_REFRESH_MINUTES,
                    )
                )
                * 2,
                int(
                    config_entry.options.get(
                        CONF_FALLBACK_REFRESH_MINUTES,
                        DEFAULT_FALLBACK_REFRESH_MINUTES,
                    )
                )
                * 3,
            )
        )

    async def _async_setup(self) -> None:
        """Perform one-time startup work."""
        try:
            user = await self.client.async_get_user()
            hierarchy = await self.client.async_get_hierarchy()
        except AprilaireCloudInvalidCredentialsError as err:
            raise ConfigEntryAuthFailed from err
        except AprilaireCloudRateLimitError as err:
            raise UpdateFailed(
                "AprilAire REST API rate limited during setup",
                retry_after=err.retry_after,
            ) from err
        except (
            AprilaireCloudApiError,
            AprilaireCloudAuthenticationProtocolError,
            AprilaireCloudAuthenticationTransientError,
            AprilaireCloudCommunicationError,
        ) as err:
            raise UpdateFailed(f"Unable to initialize AprilAire integration: {err}") from err

        self._user_id = str(user["userId"])
        self._email = user.get("email", self.client.username)
        self._apply_hierarchy(hierarchy)
        self.data = self._build_snapshot()

        await self._async_sync_location_websockets(wait_for_ready=True)
        refreshed_ids, refresh_errors = await self._async_rest_refresh_devices(
            self._device_ids_requiring_rest_refresh()
        )
        blocking_errors = self._errors_without_healthy_push(refresh_errors)
        if blocking_errors and not refreshed_ids:
            self._raise_refresh_error(next(iter(blocking_errors.values())))
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
                blocking_errors = self._errors_without_healthy_push(refresh_errors)
                if blocking_errors and not refreshed_ids:
                    self._raise_refresh_error(next(iter(blocking_errors.values())))

            await self._async_cleanup_removed_locations(removed_location_ids)
            await self._async_cleanup_removed_devices(removed_ids)
            self._update_support_issue()
        except AprilaireCloudInvalidCredentialsError as err:
            raise ConfigEntryAuthFailed from err
        except AprilaireCloudRateLimitError as err:
            raise UpdateFailed(
                "AprilAire REST API rate limited",
                retry_after=err.retry_after,
            ) from err
        except (
            AprilaireCloudAuthenticationProtocolError,
            AprilaireCloudAuthenticationTransientError,
            AprilaireCloudCommunicationError,
            AprilaireCloudApiError,
        ) as err:
            raise UpdateFailed(f"Unable to refresh AprilAire data: {err}") from err

        snapshot = self._build_snapshot()
        self._update_refresh_interval()
        return snapshot

    async def async_shutdown(self) -> None:
        """Tear down runtime resources."""
        if self._refresh_event_task is not None:
            self._refresh_event_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._refresh_event_task
            self._refresh_event_task = None
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
            message_changed, message_support_changed = self._process_message(message)
            changed = changed or message_changed
            support_changed = support_changed or message_support_changed

        if changed:
            if support_changed:
                self._update_support_issue()
            self._last_websocket_message_at[location_id] = datetime.now(tz=UTC)
            self._publish_snapshot()

    def _process_message(self, message: dict[str, Any]) -> tuple[bool, bool]:
        """Apply one push message and report state/support changes."""
        message_type = message.get("_type")
        if message_type == "RefreshEvent":
            LOGGER.debug("RefreshEvent received")
            self._schedule_refresh()
            return False, False

        device_id = message.get("deviceId")
        if device_id is None:
            return False, False
        LOGGER.debug("WebSocket message received: %s", message_type)
        if device_id not in self._devices:
            LOGGER.debug("Caching message for an unknown device")
            self._cache_unknown_device_message(device_id, message)
            self._schedule_refresh()
            return False, False

        record = self._devices[device_id]
        updated = evaluate_device_support(apply_device_message(record, message))
        updated = replace(
            updated,
            health=replace(
                updated.health,
                last_push_received_at=datetime.now(tz=UTC),
            ),
        )
        if updated == record:
            return False, False
        self._devices[device_id] = updated
        self._confirm_inflight_settings(message_type, device_id, updated)
        support_changed = (
            updated.supported != record.supported
            or updated.unsupported_reason != record.unsupported_reason
        )
        return True, support_changed

    def _confirm_inflight_settings(
        self,
        message_type: Any,
        device_id: str,
        record: DeviceRecord,
    ) -> None:
        """Signal a pending write when confirmed by DeviceSettings."""
        if message_type != "DeviceSettings":
            return
        write_state = self._write_states.get(device_id)
        profile = get_profile(record.profile_key)
        if (
            write_state is not None
            and write_state.inflight_event is not None
            and write_state.inflight_command is not None
            and profile is not None
            and profile.command_confirmed(record, write_state.inflight_command)
        ):
            write_state.inflight_event.set()

    async def async_socket_state_changed(self, state: SocketState) -> None:
        """Track websocket connection state."""
        self._socket_states[state.location_id] = state
        self._publish_snapshot()

    def device_is_available(self, device_id: str) -> bool:
        """Return source-aware availability for one device."""
        record = self._devices.get(device_id)
        if record is None or not record.supported or record.health.offline:
            return False
        socket = self._socket_states.get(record.hierarchy.location_id)
        if (
            socket is not None
            and socket.transport_connected
            and socket.initial_sync_complete
        ):
            return True
        last_rest = record.health.last_rest_received_at
        return (
            last_rest is not None
            and datetime.now(tz=UTC) - last_rest <= self._rest_freshness_window
        )

    async def async_execute_command(
        self,
        device_id: str,
        command: DeviceCommand,
    ) -> None:
        """Validate, encode, and execute a profile-owned command."""
        record = self._devices.get(device_id)
        if record is None:
            raise AprilaireCloudWriteError("AprilAire device is unavailable")
        profile = get_profile(record.profile_key)
        if profile is None:
            raise AprilaireCloudWriteError("AprilAire device profile is unavailable")
        encoded = profile.encode_command(record, command)
        await self._async_write_device_settings(device_id, encoded)

    async def _async_write_device_settings(
        self,
        device_id: str,
        encoded: EncodedCommand,
    ) -> None:
        """Write one validated command and reconcile normalized intent."""
        payload = encoded.payload
        write_state = self._write_states.setdefault(device_id, DeviceWriteState())
        payload_paths = format_leaf_paths(payload)
        LOGGER.debug("Device write started for paths: %s", payload_paths)
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
                write_state.inflight_command = encoded.command
                write_state.inflight_event = inflight_event

                await self.client.async_patch_device_settings(device_id, payload)
                try:
                    await asyncio.wait_for(
                        inflight_event.wait(), timeout=POST_WRITE_CONFIRM_TIMEOUT
                    )
                    LOGGER.debug("Device write confirmed via WebSocket")
                    return
                except TimeoutError:
                    LOGGER.debug("Device write confirmation timed out; checking REST")
                    for attempt in range(POST_WRITE_RECONCILIATION_ATTEMPTS):
                        await self._async_refresh_device_settings(device_id)
                        self._publish_snapshot()
                        record = self._devices.get(device_id)
                        profile = get_profile(record.profile_key) if record else None
                        if (
                            record is not None
                            and profile is not None
                            and profile.command_confirmed(record, encoded.command)
                        ):
                            LOGGER.debug("Device write confirmed via REST")
                            return
                        if not self._pending_payload_is_current(device_id, payload):
                            return
                        if attempt + 1 < POST_WRITE_RECONCILIATION_ATTEMPTS:
                            await asyncio.sleep(
                                POST_WRITE_RECONCILIATION_DELAY_SECONDS
                            )

                    should_raise = True
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
            write_state.inflight_command = None
            write_state.inflight_event = None
            self._sync_write_state(device_id, confirmed_settings=None)

        if should_raise:
            self._clear_pending_device_settings(device_id, payload)
            self._sync_write_state(device_id, confirmed_settings=None)
            self._publish_snapshot()
            raise AprilaireCloudWriteError("AprilAire did not confirm updated settings")

    def _build_snapshot(self) -> AprilaireSnapshot:
        """Build an immutable snapshot for entities."""
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
            LOGGER.debug("%d device(s) added to hierarchy", len(new_device_ids))
        if removed_ids:
            LOGGER.debug("%d device(s) removed from hierarchy", len(removed_ids))
        for device_id in removed_ids:
            self._write_states.pop(device_id, None)
            self._unknown_device_messages.pop(device_id, None)
            for cache in (
                self._optional_404_fingerprints,
                self._optional_endpoint_failures,
            ):
                for cache_key in tuple(cache):
                    if cache_key[0] == device_id:
                        cache.pop(cache_key, None)
        return removed_ids

    def _errors_without_healthy_push(
        self,
        errors: dict[str, Exception],
    ) -> dict[str, Exception]:
        """Return failures not covered by a synchronized push connection."""
        return {
            device_id: error
            for device_id, error in errors.items()
            if (
                (record := self._devices.get(device_id)) is None
                or (
                    (socket := self._socket_states.get(record.hierarchy.location_id)) is None
                    or not socket.transport_connected
                    or not socket.initial_sync_complete
                )
            )
        }

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
        """Hydrate devices through independent critical and optional REST stages."""
        ids = list(device_ids)
        if not ids:
            return set(), {}
        LOGGER.debug("REST refresh starting for %d device(s)", len(ids))

        semaphore = asyncio.Semaphore(MAX_PARALLEL_REST_REQUESTS)
        results = await asyncio.gather(
            *(self._async_rest_refresh_device(device_id, semaphore) for device_id in ids)
        )
        refreshed_ids: set[str] = set()
        refresh_errors: dict[str, Exception] = {}

        for device_id, progressed, critical_errors in results:
            if progressed:
                refreshed_ids.add(device_id)
            elif critical_errors:
                error = critical_errors[0]
                LOGGER.warning(
                    "Critical REST hydration failed for a device (%s)",
                    _classify_rest_failure(error).value,
                )
                refresh_errors[device_id] = error
        if refreshed_ids:
            self._last_rest_refresh_at = datetime.now(tz=UTC)
        return refreshed_ids, refresh_errors

    async def _capture_rest_result(
        self,
        semaphore: asyncio.Semaphore,
        request: Awaitable[dict[str, Any]],
    ) -> dict[str, Any] | Exception:
        """Run one bounded REST request and retain only typed failures."""
        try:
            async with semaphore:
                return await request
        except (
            AprilaireCloudApiError,
            AprilaireCloudAuthenticationError,
            AprilaireCloudCommunicationError,
            AprilaireCloudRateLimitError,
        ) as err:
            return err

    async def _async_rest_refresh_device(
        self,
        device_id: str,
        semaphore: asyncio.Semaphore,
    ) -> tuple[str, bool, list[Exception]]:
        """Hydrate one device in critical then profile-specific stages."""
        status_result, settings_result = await asyncio.gather(
            self._capture_rest_result(
                semaphore, self.client.async_get_device_status(device_id)
            ),
            self._capture_rest_result(
                semaphore, self.client.async_get_device_settings(device_id)
            ),
        )
        if device_id not in self._devices:
            return device_id, False, []

        progressed, critical_errors = self._apply_critical_rest_results(
            device_id, status_result, settings_result
        )
        record = self._devices[device_id]
        fingerprint = self._optional_request_fingerprint(record)
        requests = [
            request
            for request in status_requests_for_record(record)
            if self._optional_404_fingerprints.get((device_id, request.key))
            != fingerprint
        ]
        optional_results = await asyncio.gather(
            *(
                self._capture_rest_result(
                    semaphore,
                    self.client.async_get_status(device_id, request.endpoint),
                )
                for request in requests
            )
        )
        progressed = self._apply_optional_rest_results(
            device_id,
            requests,
            optional_results,
            fingerprint,
        ) or progressed
        if progressed:
            self._mark_rest_progress(device_id)
        return device_id, progressed, critical_errors

    def _apply_critical_rest_results(
        self,
        device_id: str,
        status_result: dict[str, Any] | Exception,
        settings_result: dict[str, Any] | Exception,
    ) -> tuple[bool, list[Exception]]:
        """Apply independent critical status/settings responses."""
        errors: list[Exception] = []
        progressed = False
        if isinstance(status_result, Exception):
            errors.append(status_result)
        else:
            self._devices[device_id] = evaluate_device_support(
                apply_rest_device_status(self._devices[device_id], status_result)
            )
            progressed = True
        if isinstance(settings_result, Exception):
            errors.append(settings_result)
        else:
            self._devices[device_id] = evaluate_device_support(
                apply_full_device_settings(self._devices[device_id], settings_result)
            )
            self._sync_write_state(device_id, confirmed_settings=settings_result)
            progressed = True
        return progressed, errors

    @staticmethod
    def _optional_request_fingerprint(record: DeviceRecord) -> str:
        """Fingerprint facts that determine optional endpoint support."""
        return repr(
            (
                record.profile_key,
                record.hierarchy.access,
                record.hierarchy.zone,
                record.device_setup,
                record.device_settings,
            )
        )

    def _apply_optional_rest_results(
        self,
        device_id: str,
        requests,
        results: list[dict[str, Any] | Exception],
        fingerprint: str,
    ) -> bool:
        """Apply optional results independently and cache unsupported routes."""
        progressed = False
        for request, result in zip(requests, results, strict=True):
            cache_key = (device_id, request.key)
            if isinstance(result, Exception):
                failure_kind = _classify_rest_failure(result)
                self._optional_endpoint_failures[cache_key] = failure_kind
                if failure_kind is RestFailureKind.UNSUPPORTED:
                    self._optional_404_fingerprints[cache_key] = fingerprint
                LOGGER.debug("Optional REST endpoint failed (%s)", failure_kind.value)
                continue
            self._optional_endpoint_failures.pop(cache_key, None)
            self._optional_404_fingerprints.pop(cache_key, None)
            self._devices[device_id] = evaluate_device_support(
                apply_status_payload(
                    self._devices[device_id],
                    request.key,
                    result,
                    source=StateSource.REST,
                    full=True,
                )
            )
            progressed = True
        return progressed

    def _mark_rest_progress(self, device_id: str) -> None:
        """Record per-device REST freshness after any usable progress."""
        record = self._devices.get(device_id)
        if record is None:
            return
        self._devices[device_id] = replace(
            record,
            health=replace(
                record.health,
                last_rest_received_at=datetime.now(tz=UTC),
            ),
        )

    async def _async_refresh_device_settings(self, device_id: str) -> dict[str, Any]:
        """Refresh only the writable settings for one device."""
        settings = await self.client.async_get_device_settings(device_id)
        self._apply_full_device_settings(device_id, settings)
        return settings

    async def _async_cleanup_removed_devices(self, removed_ids: set[str]) -> None:
        """Remove stale entity registry entries for devices no longer in the hierarchy."""
        if not removed_ids:
            return
        LOGGER.debug("Cleaning up %d removed device(s)", len(removed_ids))
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
        LOGGER.debug("Cleaning up %d removed location(s)", len(removed_location_ids))
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
        if isinstance(err, AprilaireCloudInvalidCredentialsError):
            raise ConfigEntryAuthFailed from err
        if isinstance(err, AprilaireCloudRateLimitError):
            raise UpdateFailed(
                "AprilAire REST API rate limited",
                retry_after=err.retry_after,
            ) from err
        if isinstance(
            err,
            (
                AprilaireCloudAuthenticationProtocolError,
                AprilaireCloudAuthenticationTransientError,
                AprilaireCloudCommunicationError,
                AprilaireCloudApiError,
            ),
        ):
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
