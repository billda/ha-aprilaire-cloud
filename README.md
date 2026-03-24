# AprilAire Cloud

Home Assistant custom integration for AprilAire Healthy Air cloud-connected dehumidifiers.

This integration connects to the modern `aprilaire.io` cloud platform used by the AprilAire Healthy Air app. It is designed as a standard Home Assistant config-entry integration for HACS, with automatic device discovery, WebSocket-first updates, diagnostics support, and dynamic entity creation when new supported devices appear on the account.

> [!NOTE]
> This project is focused on doing one thing well: exposing supported AprilAire cloud dehumidifiers in Home Assistant with a websocket-first update model and a clean config-entry setup.

## At A Glance

| Area | Details |
| --- | --- |
| Platform | Home Assistant custom integration via HACS |
| Cloud | `aprilaire.io` |
| Device focus | Supported AprilAire Healthy Air dehumidifiers |
| Setup style | UI-only config entry |
| Update model | WebSocket-first with bounded REST fallback |
| Multi-device support | Yes, across multiple locations on one account |

## Highlights

- **Easy setup:** config-entry setup from the Home Assistant UI, with no YAML required
- **Fast updates:** WebSocket-first updates for near real-time state changes
- **Account-aware discovery:** automatically finds supported dehumidifiers on the configured AprilAire account
- **Good HA behavior:** device registry support, reauth flow, diagnostics, and dynamic entity creation
- **Safer operation:** token refresh, bounded REST fallback, rate-limit handling, and redacted diagnostics
- **Better UX:** setup validation for unsupported accounts, repair issues for mixed accounts, and options for refresh tuning

## What This Integration Supports

This integration supports AprilAire Healthy Air dehumidifiers available through the `aprilaire.io` cloud API when they expose:

- equipment type `dehumidifier`
- `controlType == internal`
- `scale == %RH`
- a writable `humiditySetpoint`

The implementation is meant to work across multiple compatible AprilAire dehumidifier models on the same account, including multiple devices spread across multiple locations. The integration was built to discover devices by capability rather than by a hardcoded model allowlist.

Live testing has been performed against a real AprilAire cloud account and a real cloud-connected AprilAire dehumidifier. The code is intentionally conservative about what it exposes: if a device does not match the supported capability profile, it is ignored rather than partially supported in a broken or misleading way.

> [!TIP]
> If your account authenticates successfully but Home Assistant still shows no devices, the most common reason is that the device uses an unsupported capability profile rather than bad credentials.

## Explicitly Out Of Scope

The following are not supported:

- `aprilairestat.com`
- AprilAire thermostats
- devices that use `drynessSetpoint`
- devices using `remote`, `external`, or other non-internal control modes
- devices using dew-point style control instead of `%RH`
- manual YAML configuration

> [!WARNING]
> This integration does not try to approximate unsupported AprilAire devices. Unsupported capability profiles are intentionally skipped rather than exposed in a misleading or partially broken way.

## Installation

### HACS

Recommended for most users.

1. Open HACS in Home Assistant.
2. Add this repository as a custom repository of type `Integration`.
3. Install `AprilAire Cloud`.
4. Restart Home Assistant.
5. Go to `Settings > Devices & services`.
6. Click `Add Integration`.
7. Search for `AprilAire Cloud`.
8. Enter the same email address and password you use in the AprilAire Healthy Air app.

### Manual

1. Copy `custom_components/aprilaire_cloud` into your Home Assistant `custom_components` directory.
2. Restart Home Assistant.
3. Add the integration from `Settings > Devices & services`.

## Configuration

Setup is entirely UI-driven.

### Configuration Options

After setup, use the integration's `Configure` action to adjust:

- safety refresh interval
- fallback refresh interval while websocket connectivity is degraded
- whether extra diagnostic entities should be enabled by default for newly created devices

### What Setup Does

During setup, the integration:

1. Authenticates against AprilAire Cognito.
2. Calls the AprilAire account API to validate the account.
3. Loads the AprilAire device hierarchy for the account.
4. Creates a single Home Assistant config entry for that AprilAire cloud account.
5. Discovers and adds all supported dehumidifiers under that account.

If the account authenticates successfully but contains no supported devices, setup stays on the form and explains the supported device profile. If the account contains a mix of supported and unsupported devices, setup continues and Home Assistant creates a repair issue summarizing what was skipped.

The account `userId` is used as the unique config-entry identifier, which prevents the same AprilAire account from being added twice.

## Credentials And Authentication

- Credentials are stored in the Home Assistant config entry, not in YAML.
- Access, ID, and refresh tokens are kept in memory only.
- Tokens are refreshed automatically before expiry.
- If the API returns `401 Unauthorized`, the integration attempts token recovery automatically.
- If recovery fails, Home Assistant reauthentication is triggered using the standard config-entry reauth flow.
- You can also update credentials later from the integration's standard reconfigure flow.

## Device Discovery And Auto-Add Behavior

One Home Assistant config entry represents one AprilAire cloud account.

All supported dehumidifiers on that account are created automatically during setup. If you add another supported AprilAire dehumidifier to the same account later, Home Assistant will surface it automatically without requiring you to remove and re-add the integration.

Discovery happens through:

- the initial hierarchy load during setup
- periodic hierarchy refreshes
- WebSocket-driven refresh events

Unsupported devices remain hidden rather than creating incomplete entities.

## Entities

The exact set of entities depends on what each device reports through the AprilAire API.

### Entity Overview

| Entity type | Purpose |
| --- | --- |
| `humidifier` | Main dehumidifier control and target humidity |
| `sensor` | Humidity, temperature, filter life, diagnostics |
| `binary_sensor` | Alerts and running-state diagnostics |
| `number` | Writable alert thresholds when supported |

### Primary Entity

Each supported unit creates one primary `humidifier` entity using Home Assistant's dehumidifier device class.

Supported controls:

- turn the dehumidifier on
- turn the dehumidifier off
- set target humidity

Reported state includes:

- current controlling humidity
- target humidity setpoint
- operating action when available

Action mapping is based on AprilAire `equipmentStatus`:

- `dehumidifying` and `defrosting` map to drying
- `inactive` and `air-sampling` map to idle
- off mode maps to off

### Sensor Entities

Depending on the device payload, the integration may create:

- current humidity
- current temperature
- filter life remaining
- fan runtime hours
- Wi-Fi RSSI
- raw equipment status
- additional non-controlling temperature sensors

Diagnostic sensors such as Wi-Fi signal, fan runtime, equipment status, and extra temperature probes are disabled by default when they are likely to be noisy or low-value for most users.

### Binary Sensor Entities

Depending on the device payload, the integration may create:

- filter service needed
- high humidity alert
- low humidity alert
- high temperature alert
- low temperature alert
- compressor running
- dehumidifier fan running
- HVAC fan running

Operational diagnostic binary sensors are disabled by default.

### Number Entities

When exposed by the API, the integration creates writable number entities for supported alert thresholds.

Currently implemented:

- high humidity alert limit

## Device Information

Each physical AprilAire unit is registered as a Home Assistant device with:

- manufacturer: `AprilAire`
- identifiers based on the AprilAire cloud `deviceId`
- model from the cloud API
- firmware version from the cloud API
- hardware version when available
- suggested area based on the AprilAire room assignment

Device names are derived from the AprilAire hierarchy, typically using location name, room name, and model.

## Update Model

This integration is `cloud_push` and uses WebSockets as the primary transport.

### Update Flow

1. Open one WebSocket connection per AprilAire location.
2. Subscribe to that location using the current ID token.
3. Process push updates such as device status and device settings changes.
4. Use slower REST refreshes only when needed for discovery, reconciliation, or degraded websocket health.

This allows Home Assistant to reflect most state changes without waiting for a polling interval.

### Safety Refresh

A slower REST-based hierarchy refresh runs periodically to keep discovery and topology aligned with the account.

### Fallback Refresh

If one or more WebSocket connections become unhealthy, the integration temporarily falls back to bounded REST refreshes until push connectivity is restored.

### Writes

Commands such as turning the device on or changing target humidity are sent with REST `PATCH` requests to the AprilAire settings endpoint. After a successful write, the integration waits for the corresponding WebSocket update. If a confirming push message does not arrive quickly, it performs one targeted REST reconciliation read.

## Rate Limiting

AprilAire's public rate limits are not documented, so the integration takes a defensive approach.

- `429 Too Many Requests` responses are honored
- `Retry-After` is parsed and clamped to a sane range
- nonessential background REST activity backs off automatically
- short user-initiated writes may be retried once if the throttle window is very small
- longer throttling windows fail fast so Home Assistant can present a clear temporary error to the user

WebSocket reconnect backoff is handled separately from REST API throttling.

## Supported Home Assistant Behavior

This integration uses standard Home Assistant patterns wherever possible:

- config entries
- device registry
- entity registry
- reauth flow
- reconfigure flow
- diagnostics support
- dynamic device and entity creation after setup
- config-entry-only setup

That means supported devices should appear in `Settings > Devices & services` and new supported devices added to the same AprilAire account should also surface automatically.

## Limitations

- Support depends on reverse-engineered AprilAire cloud behavior rather than official vendor documentation.
- Only devices with an internal `%RH` humidity setpoint are exposed.
- Unsupported device capabilities are intentionally ignored rather than approximated.
- Some entities only appear when the upstream API actually reports the relevant data.

## Troubleshooting

### The integration adds no devices

Possible reasons:

- the account has no devices on `aprilaire.io`
- the account only has unsupported AprilAire equipment
- the device uses `drynessSetpoint` instead of `humiditySetpoint`
- the device uses a non-internal control mode

### A device was added in the AprilAire app but does not show up immediately

The integration should discover it automatically, but discovery depends on either a refresh event or the next hierarchy refresh. If it does not appear after a reasonable interval, reload the integration once from Home Assistant.

### State changes are delayed

If WebSocket connectivity is interrupted, the integration falls back to periodic REST refreshes until push connectivity returns. During that period, updates may feel less immediate.

### Authentication stopped working

The integration automatically refreshes tokens and will trigger Home Assistant reauthentication if credentials are no longer valid. Re-enter the same AprilAire Healthy Air account credentials when prompted.

## Diagnostics

The integration includes a Home Assistant diagnostics handler.

Diagnostics are intended to help debug:

- discovered locations
- discovered devices
- whether a device was considered supported
- the current device snapshot
- WebSocket connection state
- registered Home Assistant devices and entities

Sensitive values such as usernames, passwords, tokens, account identifiers, and location names are redacted or hashed in diagnostics output.

## Development Notes

- `.env` is for local developer testing only and must never be committed.
- Reverse-engineering and API exploration scripts in this repository are development tools only and are not used by the production integration code.
- The integration does not depend on those scripts at runtime.

## Status

This project is an independent Home Assistant custom integration and is not an official AprilAire product.
