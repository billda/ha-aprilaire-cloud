# AprilAire Cloud protocol evidence

This catalog records what the integration knows about the undocumented
`aprilaire.io` protocol and why it enables or disables each behavior. It
supersedes broad protocol claims in the March 2026 local research notes.

Last reviewed: 2026-07-20.

## Evidence levels

| Level | Meaning |
| --- | --- |
| `live_confirmed` | A physical device behavior was confirmed through a before/after test or a named community tester report. |
| `captured` | A field is present in a sanitized real payload, but its behavior was not actively exercised. |
| `decompiled` | A fact was found in a named Healthy Air app version and has not been observed live. |
| `inferred` | A conservative interpretation is needed for safe parsing or compatibility. |
| `unknown` | Evidence is insufficient. The integration does not enable a write from this evidence. |

The synthetic fixtures in [`tests/fixtures`](../tests/fixtures) have
machine-readable provenance in
[`tests/fixtures/evidence.json`](../tests/fixtures/evidence.json). Account,
device, location, room, sensor, firmware, and hardware values were replaced.

## Sources and tested matrix

| Source | App version | Model / firmware | What it establishes |
| --- | --- | --- | --- |
| Maintainer capture and device tests | Healthy Air Android 2.23.58 | E100W / firmware replaced | Hierarchy and initial push shapes; standalone dehumidifier status; on/off, `%RH` target, and high-humidity alert writes |
| [Issue 3](https://github.com/billda/ha-aprilaire-cloud/issues/3) | Unknown | E100W external-control configuration / unknown firmware | External-control devices retain useful telemetry and may expose the app's explicit on/off setting, but a humidity target is not applicable |
| [PR 5](https://github.com/billda/ha-aprilaire-cloud/pull/5), inspected as evidence only | Unknown | 8920W / unknown firmware | `tempSensors`, `humSensors`, separate heating/cooling/fan state, zone settings, observed mode/fan/hold values |
| [PR 6](https://github.com/billda/ha-aprilaire-cloud/pull/6), inspected as evidence only | Unknown | 8920W / unknown firmware | `DeviceEvent` offline and rescinded messages |
| [PR 7](https://github.com/billda/ha-aprilaire-cloud/pull/7), inspected as evidence only | Unknown | 8920W with attached humidifier / unknown firmware | Global attached-humidifier settings/status, power/target writes, and water-panel service state |
| Android APK inspection | Healthy Air Android 2.23.58 | Not device-specific | Cognito configuration, REST/WebSocket route names, and the existence of an account `/login` route |

The listed iOS 2.27.16 release was not decompiled or captured for this work and
is not protocol evidence. No claim here implies support for every device shown
in the mobile app.

## State and event contracts

- REST hierarchy, device setup, device status, device settings, and
  profile-status responses are treated as full snapshots of their section.
- WebSocket `DeviceSettings` and device/profile status messages are treated as
  incremental. Nested dictionaries merge and observed lists replace the prior
  list snapshot.
- `asOf` is parsed per logical section. Older input cannot replace newer
  state; at an equal timestamp a confirmed WebSocket update takes precedence
  over REST.
- A WebSocket empty JSON array is a valid data batch. Connection,
  subscription acknowledgment, and initial synchronization are separate
  lifecycle states.
- A `DeviceEvent` with `type == "offline"`, `occurred`, and no `rescinded`
  marks that device offline. A newer matching event with `rescinded` restores
  it. Stale events are ignored.
- One WebSocket connection is maintained per location. Fresh REST data can
  keep a device available during a location socket failure; an explicit
  offline event overrides retained values.

These full/incremental rules are conservative compatibility rules. They are not
a claim that the vendor has published a formal schema.

## Authentication evidence

The Android 2.23.58 app configuration and existing integration behavior
establish Cognito SRP authentication with in-memory ID, access, and refresh
tokens. The integration:

- reuses a valid ID token;
- renews tokens before expiry under a single-flight lock;
- falls back to a full SRP login when a refresh token is rejected;
- attempts a jittered proactive full login after approximately 25 days;
- retains a successful refresh if that proactive login fails transiently;
- maps only definite account/credential failures to Home Assistant reauth;
- treats network, throttling, service, malformed-response, and unknown failures
  as retryable setup/update failures.

The app contains a `PUT https://account.aprilaire.io/login` route
(`decompiled`). Its current headers, request body, response contract, and
lifetime effect remain `unknown`. Production code does not call it.

## Read and write matrix

All writes additionally require explicit hierarchy `manage` access and the
observed settings key for that device. Missing or unknown access is read-only.

| Device behavior | Evidence | Integration behavior |
| --- | --- | --- |
| E100W on/off | `live_confirmed` maintainer test | Enabled when `mode` is reported |
| E100W internal `%RH` target | `live_confirmed` maintainer test | Enabled only for internal `%RH` control with `humiditySetpoint` |
| E100W high-humidity alert | `live_confirmed` maintainer test | Enabled only when the corresponding nested setting is reported |
| External/remote dehumidifier telemetry | `live_confirmed` community report for external control | Read sensors whose fields are present |
| External/remote dehumidifier on/off | `live_confirmed` community report for external control | Exposed as an on/off switch only when `mode` is explicitly reported |
| External/remote humidity/dew-point target | `unknown` / inapplicable | Disabled |
| 8920W temperature/humidity and HVAC action | `live_confirmed` community report | Read from observed sensor arrays and separate heat/cool/fan fields |
| 8920W mode, fan, and hold | `live_confirmed` community report | Enabled only for model 8920W and observed key style/value set |
| 8920W heat/cool setpoints | Read keys `live_confirmed`; PATCH unit/deadband `unknown` | Values are read; writes are disabled |
| Attached central humidifier power/target | `live_confirmed` community report | One thermostat-global entity, enabled only when installation/settings are explicit |
| Attached humidifier water panel | `live_confirmed` community report | Remaining/service state exposed only when reported |
| Other thermostat-attached IAQ equipment | Captured/community schema | Read-only status/service entities when installation is explicit |
| Emergency heat and schedule editing | Outside scope | Disabled |

No value-threshold temperature inference is used. A protocol-provided unit is
used when present; otherwise Home Assistant's configured unit is only a display
fallback. It is not evidence of the PATCH unit and does not enable a setpoint
write.

## Open evidence gaps

- 8920W sensor/settings units, PATCH units, allowed temperature range, and
  heat/cool deadband need a sanitized before/after capture from the same
  device.
- Firmware versions and current mobile-app versions for the community 8920W
  reports are unknown.
- The current account `/login` request contract and purpose are unknown.
- External/remote/dew-point control behavior beyond the reported sensor and
  explicit mode fields is unknown.
- Other thermostat and IAQ model families need independent captures; shared
  mobile-app code is not sufficient evidence.

Until those gaps are closed, the corresponding writes remain disabled rather
than being approximated.
