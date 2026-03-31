# ha-aprilaire

Home Assistant custom integration for AprilAire Cloud dehumidifiers.

## Branch

Default branch: `main`

## Commands

- **Test:** `pytest`
- **Lint:** `ruff check .`
- **Type check:** `mypy custom_components/aprilaire_cloud` (on CI with Python 3.13)
- **Compile check:** `python3 -m py_compile custom_components/aprilaire_cloud/*.py`
- **Post-push:** Check GitHub Actions `Validate` workflow

## Architecture

- Frozen dataclasses for all data models (`models.py`)
- Pure state reducers in `state.py` — no side effects, return new records
- `coordinator.py` orchestrates WebSocket-first updates with REST fallback
- `profiles.py` handles device classification and entity mapping
- Entity platforms use `setup_dynamic_platform_entities` for add/remove lifecycle

## Conventions

- Use `LOGGER` from `const.py` for all logging
- DEBUG for expected flow, INFO for mode changes, WARNING for recoverable errors
- Never log sensitive data (tokens, credentials, email)
- Include device_id/location_id in log messages for filtering
