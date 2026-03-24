# Contributing

Thanks for taking a look at `ha-aprilaire-cloud`.

This project started as an integration for my own Home Assistant setup, and the most valuable contributions right now are:

- testing with additional AprilAire dehumidifier models
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
.venv/bin/pytest
python3 -m py_compile custom_components/aprilaire_cloud/*.py tests/*.py
```

## Testing In Home Assistant

For real-world testing, copy `custom_components/aprilaire_cloud` into a Home Assistant config directory and add the integration through `Settings > Devices & services`.

This integration talks to a live, undocumented AprilAire cloud API, so real-device testing is important.

If you are testing with a model other than the AprilAire E100W, please report:

- model name
- firmware version if available
- whether the device was auto-discovered
- what entities showed up
- whether target humidity and on/off control worked
- whether updates stayed in sync with the AprilAire app

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

Please redact anything sensitive before posting logs manually.

## Compatibility Reports

If your device is discovered but only partly works, or if it does not show up at all, open a model compatibility report rather than assuming the integration simply does not support it. The AprilAire API is undocumented and capability differences between models matter.

## Pull Requests

A good PR for this repository is:

- focused on one change
- covered by tests where practical
- consistent with Home Assistant integration patterns
- small enough to review without guessing at intent

Please avoid bundling unrelated refactors with behavioral changes.

If your PR changes user-facing behavior, include:

- what changed
- why it changed
- how you tested it

## Support Routing

- Questions and general discussion: GitHub Discussions
- Bugs and regressions: GitHub Issues
- Code changes: Pull Requests
