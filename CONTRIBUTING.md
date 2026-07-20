# Contributing

Thanks for taking a look at `ha-aprilaire-cloud`.

This project started as an integration for my own Home Assistant setup, and the most valuable contributions right now are:

- testing with additional AprilAire dehumidifier models
- evidence-backed 8920W and thermostat-attached IAQ validation
- clear bug reports with diagnostics
- fixes for auth, WebSocket, and write-path edge cases
- documentation improvements
- focused test coverage improvements

## Development Setup

1. Clone the repository.
2. Create and activate a virtual environment.
3. Install the test dependencies.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements_test.txt
```

## Local Checks

Run these before opening a pull request:

```bash
.venv/bin/ruff check .
.venv/bin/mypy custom_components/aprilaire_cloud
.venv/bin/coverage run --source=custom_components/aprilaire_cloud -m pytest
.venv/bin/coverage report -m --precision=2 --fail-under=95.01
.venv/bin/python -m compileall custom_components/aprilaire_cloud tests
git diff --check
```

## Testing In Home Assistant

For real-world testing, copy `custom_components/aprilaire_cloud` into a Home Assistant config directory and add the integration through `Settings > Devices & services`.

This integration talks to a live, undocumented AprilAire cloud API, so real-device testing is important.

If you are testing with a model other than the AprilAire E100W, please report:

- model name
- firmware version if available
- account access level (`manage`, shared/read-only, or unknown)
- whether the device was auto-discovered
- what entities showed up
- which controls Home Assistant actually advertised
- the unit displayed by both the device/app and Home Assistant
- whether updates stayed in sync with the AprilAire app
- whether an offline/recovery cycle changed only that device's availability

Do not test a control that Home Assistant does not advertise. Thermostat
temperature setpoint writes remain disabled pending evidence for PATCH units,
limits, and deadband.

## Bug Reports

Please use the bug report issue form when possible.

Helpful information includes:

- Home Assistant version
- HACS version
- AprilAire model
- firmware version
- exact behavior you expected
- exact behavior you saw
- Home Assistant logs if there was an error
- integration diagnostics export

To download diagnostics in Home Assistant:

1. Go to `Settings > Devices & services`
2. Open `AprilAire Cloud`
3. Use the diagnostics download option from the integration menu

Generated diagnostics are designed to omit raw payloads and pseudonymize stable
identifiers within one export, but review them before public upload. Users of
older integration versions should assume diagnostics may contain identifiers.
Never post credentials, tokens, raw API responses, addresses, contractor data,
location/room names, device identifiers, or unsanitized entity IDs.

## Compatibility Reports

If your device is discovered but only partly works, or if it does not show up at all, open a model compatibility report rather than assuming the integration simply does not support it. The AprilAire API is undocumented and capability differences between models matter.

## Protocol Evidence

Read [the protocol evidence catalog](docs/protocol-evidence.md) before changing
vendor parsing or command behavior. New protocol fixtures must be synthetic or
sanitized and must record:

- evidence level: `live_confirmed`, `captured`, `decompiled`, `inferred`, or
  `unknown`;
- source and app version when known;
- model and firmware when known;
- endpoint or WebSocket message type;
- full versus incremental payload status;
- every identifier and value class replaced during sanitization.

For a write-path proposal, provide a before/after read from the same device,
including the explicit unit and constraints. Mobile-app strings or a plausible
field name alone are not enough to enable HVAC writes. Do not commit APKs,
decompiled source, private credentials, or raw captures.

## Pull Requests

A good PR for this repository is:

- focused on one change
- covered by tests where practical
- consistent with Home Assistant integration patterns
- small enough to review without guessing at intent
- capability- and account-access-gated for every write
- explicit about protocol evidence and remaining unknowns

Please avoid bundling unrelated refactors with behavioral changes.

If your PR changes user-facing behavior, include:

- what changed
- why it changed
- how you tested it

## Support Routing

- Questions and general discussion: GitHub Discussions
- Bugs and regressions: GitHub Issues
- Code changes: Pull Requests
