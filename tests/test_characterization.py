"""Compatibility and protocol-fixture characterization tests."""

from __future__ import annotations

import json
import re
from pathlib import Path

from custom_components.aprilaire_cloud.const import DOMAIN

from .common import DEVICE_ID, THERMOSTAT_DEVICE_ID

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str):
    """Load a JSON fixture."""
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_public_identity_contract_is_stable() -> None:
    """Lock the integration and released device/entity identity scheme."""
    assert DOMAIN == "aprilaire_cloud"
    assert (DOMAIN, DEVICE_ID) == ("aprilaire_cloud", "device-001")
    assert f"{DEVICE_ID}_dehumidifier" == "device-001_dehumidifier"
    assert (
        f"{THERMOSTAT_DEVICE_ID}_thermostat_pz1"
        == "device-thermostat-001_thermostat_pz1"
    )


def test_fixture_catalog_covers_required_protocol_evidence() -> None:
    """Every protocol fixture must carry complete provenance metadata."""
    evidence = _load_fixture("evidence.json")
    fixture_names = {
        path.name for path in FIXTURE_DIR.glob("*.json") if path.name != "evidence.json"
    }

    assert set(evidence["fixtures"]) == fixture_names
    for metadata in evidence["fixtures"].values():
        assert metadata["evidence"] in {
            "live_confirmed",
            "captured",
            "decompiled",
            "inferred",
            "unknown",
        }
        assert metadata["source"]
        assert metadata["app_version"]
        assert metadata["device_model"]
        assert metadata["firmware"]
        assert metadata["transport"]
        assert metadata["payload_kind"]


def test_thermostat_fixture_uses_observed_8920w_nesting() -> None:
    """The thermostat fixture reflects community-observed payload paths."""
    status = _load_fixture("thermostat_8920w_zone_status.json")

    assert isinstance(status["tempSensors"], list)
    assert isinstance(status["humSensors"], list)
    assert status["heatingStatus"] == "inactive"
    assert status["coolingStatus"] == "stage1"
    assert status["isFanOn"] is True
    assert "equipmentStatus" not in status
    assert "currentTemperature" not in status
    assert "currentHumidity" not in status


def test_all_committed_fixtures_are_obviously_synthetic() -> None:
    """Fixtures must not contain credential-like or stable real identifiers."""
    fixture_text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(FIXTURE_DIR.glob("*.json"))
    )

    assert "user@example.com" not in fixture_text
    assert "not-a-real-password" not in fixture_text
    assert not re.search(r"\b[0-9A-F]{12}\b", fixture_text)
    assert not re.search(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{12}\b",
        fixture_text,
    )
    assert "device-001" in fixture_text
    assert "location-001" in fixture_text


def test_external_control_target_behavior_remains_explicitly_unknown() -> None:
    """The fixture catalog must not promote an unverified target write."""
    evidence = _load_fixture("evidence.json")
    unknown_behavior = (
        "safe target or dew-point write semantics for externally "
        "controlled dehumidifiers"
    )

    assert unknown_behavior in evidence["unknown"]
