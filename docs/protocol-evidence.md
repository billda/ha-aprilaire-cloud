# AprilAire Cloud protocol evidence

This catalog records what the integration knows about the undocumented
`aprilaire.io` protocol and why it enables or disables each behavior. It
supersedes broad protocol claims in the March 2026 local research notes.

Last reviewed: 2026-08-11.

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
| [Issue 8](https://github.com/billda/ha-aprilaire-cloud/issues/8) and the reporter's public [`0.4.0b` branch](https://github.com/Paradox52525/ha-aprilaire-cloud/tree/0.4.0b), inspected as evidence only | Home Assistant 2026.7.2; Healthy Air app unknown | 8920W hardware / firmware 3.04.5; raw API model not captured | Native-Celsius thermostat values despite a Fahrenheit display preference, `coolingStatus: stage1`, current/setpoint extraction, and the `circ` circulation token |
| [Issue 8](https://github.com/billda/ha-aprilaire-cloud/issues/8) | Home Assistant 2026.7.2; Healthy Air app unknown | Two single-zone 8920W thermostats with attached humidifiers / firmware 3.04.5 | Attached-humidifier power/target writes, a cloud-enforced 50% maximum, sole-zone humidity context, and delayed write observation that can outlast the synchronous confirmation window |
| [Issue 8 setpoint report](https://github.com/billda/ha-aprilaire-cloud/issues/8#issuecomment-5253984537) and [fan capture](https://github.com/billda/ha-aprilaire-cloud/issues/8#issuecomment-5254035284) | Home Assistant 2026.7.2; Healthy Air app unknown | 8920W hardware / firmware 3.04.5; raw API model not captured | Mixed evidence: the reporter labels the atomic native-Celsius `heat`/`cool` PATCH examples and two-decimal encoding as `fork_code`; the 40–90°F heat and 50–93°F cool app limits, whole-Fahrenheit steps, 3°F deadband, and schedule-to-temporary-hold behavior are live-tested; HTTP 200/empty-body plus later settings-update behavior and string fan `circ` payloads are tester-reported captures |
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
- For an in-flight write, only a complete settings observation with a valid
  `asOf` that was accepted as authoritative and is strictly newer than the
  pre-write settings version is decisive. A normalized match confirms the
  command. For the evidence-gated setpoint command only, the beta currently
  infers semantic rejection from a mismatch that changes no unrelated setting,
  even when PATCH returned HTTP 200; a direct negative acknowledgment has not
  yet been captured. Other mismatches, plus partial, stale, equal, unrelated,
  and untimestamped observations, remain inconclusive and use bounded
  reconciliation.
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
| 8920W temperature/humidity and HVAC action | `live_confirmed` issue 8 and reporter branch | Exact `8920W`/`8920W_GS` contracts use native-Celsius values; read from observed sensor arrays and separate heat/cool/fan fields, including `stage1` |
| 8920W mode, fan, and hold | `live_confirmed` community report and reporter branch | Enabled only for exact `8920W`/`8920W_GS` identifiers and the observed key style/value set; captured `circ` is normalized to Home Assistant's `circulate` |
| 8920W heat/cool setpoints | PATCH keys/unit/pair/precision `captured` plus reporter `fork_code`; app limits/deadband/hold behavior `live_confirmed` | Enabled only for exact `8920W`/`8920W_GS`, `manage` access, exactly one reported zone, explicit Fahrenheit display preference, native-Celsius values, exact numeric `heat`/`cool` keys, and Home Assistant configured for Fahrenheit. Requests snap to whole °F, enforce separate reported limits and a 3°F gap, preserve rather than move the companion, and send both values atomically. Other contracts stay read-only |
| Attached central humidifier power/target | `live_confirmed` community report and issue 8 | One thermostat-global entity, enabled only when installation/settings are explicit; target maximum is 50%, while the minimum remains unknown |
| Attached humidifier water panel | `live_confirmed` community report | Remaining/service state exposed only when reported |
| Other thermostat-attached IAQ equipment | Captured/community schema | Read-only status/service entities when installation is explicit |
| Emergency heat and schedule editing | Outside scope | Disabled |

No value-threshold temperature inference is used. Exact `8920W` and
`8920W_GS` protocol identifiers use the community-confirmed native Celsius
read contract even when the device display preference is Fahrenheit. Home
Assistant performs presentation conversion. For other models, a
protocol-provided unit is required before numeric temperature state is exposed.
The setpoint write contract additionally requires exactly one reported zone,
the exact reported keys, Fahrenheit display preference, and a Fahrenheit Home
Assistant unit system; numeric magnitude alone never enables it.

## Open evidence gaps

- The reporter's raw API `DeviceStatus.model`, firmware/app versions for the
  latest setpoint capture, and exact WebSocket `_type`/`asOf` sequence are still
  needed to bind the live result to one protocol identifier and timing path.
- A genuinely invalid changed pair is still needed to confirm whether the
  newer settings event echoes the prior valid pair. The reported invalid
  example used the same 69°F/72°F pair as the valid baseline, so it is not
  decisive.
- Celsius-display PATCH units, grid, limits, and deadband behavior are not
  confirmed. Celsius-display setpoint writes remain disabled.
- Multi-zone setpoint write routing and inter-zone behavior are not captured.
  All multi-zone setpoint writes remain disabled.
- It is not proven that both targets are mandatory in every mode. The beta
  implementation sends both because that is the captured working atomic form.
- The app's automatic companion-setpoint movement needs correctly labeled
  heat/cool boundary captures. The integration deliberately rejects an invalid
  requested pair instead of inventing which companion value to move.
- The attached-humidifier minimum setpoint and multi-zone humidity source are
  unknown. No minimum is invented, and a multi-zone thermostat is not bound to
  the first zone.
- The current account `/login` request contract and purpose are unknown.
- External/remote/dew-point control behavior beyond the reported sensor and
  explicit mode fields is unknown.
- Other thermostat and IAQ model families need independent captures; shared
  mobile-app code is not sufficient evidence.

Until those gaps are closed, the corresponding writes remain disabled rather
than being approximated.
