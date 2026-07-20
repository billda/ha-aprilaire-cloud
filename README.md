# AprilAire Cloud

[![Release](https://img.shields.io/github/v/release/billda/ha-aprilaire-cloud?sort=semver)](https://github.com/billda/ha-aprilaire-cloud/releases)
[![Validate](https://github.com/billda/ha-aprilaire-cloud/actions/workflows/validate.yml/badge.svg)](https://github.com/billda/ha-aprilaire-cloud/actions/workflows/validate.yml)
[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=billda&repository=ha-aprilaire-cloud&category=integration)

Home Assistant custom integration for AprilAire Healthy Air cloud-connected
dehumidifiers and evidence-backed beta thermostat support.

This integration connects to the modern `aprilaire.io` platform used by the AprilAire Healthy Air app. It is built as a standard Home Assistant config-entry integration for HACS, with automatic device discovery, WebSocket-first updates, diagnostics support, and dynamic entity creation when new supported devices appear on the account.

## Why This Exists

I built this because I wanted my own AprilAire dehumidifier in Home Assistant and could not find an existing integration for AprilAire cloud-connected dehumidifiers anywhere on GitHub or the wider web.

The `aprilaire.io` API does not appear to be publicly documented. I figured out the routes and message shapes by reverse engineering the Android APK, and I was honestly surprised by how full-featured the cloud API is once you get into it. It has been working well for me in my own Home Assistant instance with an AprilAire E100W, so I cleaned it up and published it in the hope that other AprilAire owners can use it, test it, and help improve it.

This project is unofficial and is not affiliated with AprilAire.

## At A Glance

| Area | Details |
| --- | --- |
| Platform | Home Assistant custom integration via HACS |
| Cloud | `aprilaire.io` |
| Device focus | Capability-matched AprilAire dehumidifiers and beta 8920W thermostat support |
| Setup style | UI-only config entry |
| Update model | WebSocket-first with bounded REST fallback |
| Multi-device support | Yes, across multiple locations on one account |
| Tested live | E100W by the maintainer; selected 8920W behavior by community testers |

## Highlights

- UI-only setup through Home Assistant
- WebSocket-first updates for near real-time state changes
- Automatic discovery of supported dehumidifiers and beta thermostats on the configured account
- Support for multiple devices and multiple locations on one account
- Standard Home Assistant behavior: device registry, config entries, reauth, diagnostics, dynamic entity creation
- Granular capability detection so read-only and partial-control devices retain
  honest, useful entities
- Defensive auth refresh and rate-limit handling for an undocumented API
- Per-device offline handling and privacy-preserving diagnostics

## What This Integration Supports

The integration recognizes a dehumidifier independently of its control mode.
Entities and writes are then selected from fields the device actually reports:

- internal `%RH` control with explicit `mode` and `humiditySetpoint` receives
  the full dehumidifier entity;
- external or partial control retains understood sensors and receives an
  on/off switch only when the vendor reports an applicable `mode`;
- read/shared access receives state but no controls;
- dryness/dew-point and non-applicable humidity target writes remain disabled.

This capability model is intentionally more conservative than a model
allowlist. It does not imply that every Healthy Air device is compatible.

### Beta Thermostat Support

Thermostat support is beta. Community testing on an 8920W confirms:

- climate entities for zones `PZ1`, `SZ2`, and `SZ3` when those zones are reported
- indoor temperature/humidity from the reported sensor arrays;
- HVAC action from separate heating, cooling, and fan state;
- off, heat, cool, and auto mode writes;
- `auto`, `on`, and `circulate` fan writes;
- `none`, `temporary`, `permanent`, and `vacation` hold writes;
- read-only outdoor, equipment, service, and explicitly installed IAQ state
  when reported.

Heat and cool setpoints are displayed, but setpoint writes are deliberately
disabled: the PATCH unit and heat/cool deadband have not been proven. The
integration does not infer temperature units from numeric values.

An explicitly installed thermostat-attached humidifier receives one
thermostat-global humidifier entity rather than being assigned to an arbitrary
zone. Community-confirmed power/target keys are writable with `manage` access;
current humidity, action, and water-panel state remain unknown when not
reported.

Live validation has been performed against a real AprilAire cloud account and a real AprilAire E100W. If your device authenticates successfully but does not show up in Home Assistant, the most likely reason is that it exposes an unsupported capability profile rather than a bad login.

See [AprilAire Cloud protocol evidence](docs/protocol-evidence.md) for the
source, evidence level, and remaining gaps behind every protocol claim.

## Explicitly Out Of Scope

The following are not supported:

- `aprilairestat.com`
- dryness/dew-point target writes
- unreported or inferred control capabilities
- thermostat schedule editing
- thermostat emergency-heat writes
- YAML configuration

Unknown behavior is kept read-only or unavailable rather than approximated.

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

You can also use the My Home Assistant button above to open the repository directly in HACS.

### Manual

1. Copy `custom_components/aprilaire_cloud` into your Home Assistant `custom_components` directory.
2. Restart Home Assistant.
3. Add the integration from `Settings > Devices & services`.

## Configuration

Setup is entirely UI-driven.

During setup, the integration:

1. Authenticates against AprilAire Cognito.
2. Validates the account through the AprilAire account API.
3. Loads the AprilAire device hierarchy for the account.
4. Creates one Home Assistant config entry for that AprilAire cloud account.
5. Discovers and adds all supported dehumidifiers and beta thermostats under that account.

The account `userId` is used as the config-entry unique ID so the same AprilAire account cannot be added twice.

## Device Discovery And Auto-Add Behavior

One Home Assistant config entry represents one AprilAire cloud account.

All supported dehumidifiers and beta thermostats on that account are created automatically during setup. If you add another supported AprilAire device to the same account later, Home Assistant should surface it automatically without requiring you to remove and re-add the integration.

Discovery happens through:

- the initial hierarchy load during setup
- periodic hierarchy refreshes
- WebSocket-driven refresh events

## Entities

The exact set of entities depends on what each device reports.

| Entity type | Purpose |
| --- | --- |
| `climate` | Thermostat zone control |
| `humidifier` | Full dehumidifier control or an explicitly installed attached humidifier |
| `switch` | Honest on/off-only control for a partial-control dehumidifier |
| `sensor` | Humidity, temperature, filter life, and diagnostics |
| `binary_sensor` | Alerts and running-state diagnostics |
| `number` | Writable alert thresholds when supported |

### Primary Control

An internal `%RH` dehumidifier with applicable target and power settings
creates one primary `humidifier` entity. A device with only proven power
control receives a switch instead. A read-only device receives sensors only.

Full dehumidifier controls:

- turn the dehumidifier on
- turn the dehumidifier off
- set target humidity

### Additional Entities

Depending on device payloads, the integration may expose:

- current humidity
- current temperature
- filter life remaining
- filter service needed
- humidity and temperature alerts
- Wi-Fi RSSI
- fan runtime
- raw equipment status
- extra temperature sensors
- writable high humidity alert limit

### Thermostat And Attached-Humidifier Controls

Each supported thermostat creates one `climate` entity per reported zone.

For a confirmed 8920W contract and `manage` account access:

- set HVAC mode to off, heat, cool, or heat/cool auto
- set fan mode
- set hold/preset mode

Temperature setpoint, schedule, and emergency-heat writes are disabled. An
explicitly installed attached humidifier can expose power and humidity-target
controls plus reported water-panel service state. Other attached IAQ equipment
is read-only.

## Update Model

This integration is `cloud_push` and uses WebSockets as the primary transport.

The normal flow is:

1. Open one WebSocket connection per AprilAire location.
2. Subscribe to that location using the current ID token.
3. Merge push updates such as status and settings changes into Home Assistant.
4. Use slower REST refreshes only when needed for discovery, reconciliation, or degraded websocket health.

### Writes

Commands such as turning the device on or changing target humidity are sent with REST `PATCH` requests to the AprilAire settings endpoint. The integration then waits for the corresponding WebSocket update and falls back to a targeted REST reconciliation read if a confirming push does not arrive quickly.

### Authentication

- Credentials are stored in the Home Assistant config entry, not in YAML.
- Access, ID, and refresh tokens are kept in memory only.
- Tokens are refreshed automatically before expiry.
- A rejected refresh token falls back to a full login.
- A jittered proactive full login refreshes the long-lived session before
  normal refresh-token aging becomes user-visible.
- `401 Unauthorized` responses trigger automatic token recovery.
- Only definite credential/account failures start Home Assistant reauth;
  network, service, throttling, and unknown protocol failures remain retryable.

### Availability

Availability is tracked per device. A confirmed offline `DeviceEvent` makes
only that device unavailable, and a newer rescinded event restores it without
recreating entities. Fresh REST state may keep a device available during a
location WebSocket outage.

### Rate Limiting

AprilAire's public rate limits are not documented, so the integration behaves defensively:

- `429 Too Many Requests` responses are honored
- `Retry-After` is parsed and clamped to a sane range
- nonessential background REST activity backs off automatically
- short user-initiated writes may be retried once if the throttle window is very small
- longer throttle windows fail fast so Home Assistant can show a clear temporary error

## Support

For questions, setup help, compatibility reports, and general discussion:

- [GitHub Discussions](https://github.com/billda/ha-aprilaire-cloud/discussions)

For actionable bugs and regressions:

- [GitHub Issues](https://github.com/billda/ha-aprilaire-cloud/issues)

If you file a bug, please include:

- your AprilAire model
- firmware version if visible in Home Assistant
- Home Assistant version
- whether you have multiple devices or locations
- what action failed
- a diagnostics download from the integration if possible

Default diagnostics omit raw vendor payloads and pseudonymize identifiers
within each export. Still review the file before publishing it. Never post
credentials, tokens, raw API captures, or identifying location/device data.

## Contributing

Contributions are welcome, especially:

- compatibility testing on additional AprilAire dehumidifier models
- fixes for edge cases in auth, WebSocket handling, or writes
- docs improvements
- additional tests

Start with [CONTRIBUTING.md](CONTRIBUTING.md).

If you have a model that is not yet confirmed, please open a compatibility report even if it only partially works. That will help map out what AprilAire is exposing across the product line.

## HACS Default Status

This repository is installable today as a HACS custom repository.

I do plan to submit it to the HACS default listings, but not immediately. The current goal is to get a first round of real-world testing and feedback first, especially from owners of AprilAire dehumidifier models other than the E100W.
