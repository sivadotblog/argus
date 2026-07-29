"""Sanity-check that the sample config/checks.yaml is a valid EngineConfig."""
from __future__ import annotations

from pathlib import Path

import yaml

from healthcheck_engine.connectors import REGISTRY
from healthcheck_engine.schema import EngineConfig

CONFIG_PATH = Path(__file__).parent.parent / "config" / "checks.yaml"


def _load_config() -> EngineConfig:
    with CONFIG_PATH.open() as f:
        raw = yaml.safe_load(f)
    return EngineConfig.model_validate(raw)


def test_sample_config_parses_without_error() -> None:
    config = _load_config()
    assert isinstance(config, EngineConfig)


def test_sample_config_has_nine_checks() -> None:
    config = _load_config()
    assert len(config.checks) == 9


def test_sample_config_check_types_are_all_registered() -> None:
    config = _load_config()
    for check in config.checks:
        assert check.type in REGISTRY, f"{check.name!r} uses unregistered type {check.type!r}"


def test_sample_config_includes_all_five_new_connector_types() -> None:
    config = _load_config()
    types_present = {check.type for check in config.checks}
    assert types_present == {
        "public_api",
        "auth_api",
        "azure_status",
        "google_status",
        "vnet_capacity",
        "quota",
        "database",
    }
