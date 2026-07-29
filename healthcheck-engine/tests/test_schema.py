"""Round-trip tests for the stdout JSON contract (schema.py)."""
from __future__ import annotations

import json

from healthcheck_engine.schema import CheckResult, CheckStatus, ErrorType, Heartbeat


def test_check_result_defaults_and_schema_version() -> None:
    result = CheckResult(source="github-api", check_type="public_api", status=CheckStatus.OK)

    assert result.schema_version == 1
    assert result.event_type == "check_result"
    assert result.source == "github-api"
    assert result.check_type == "public_api"
    assert result.status == CheckStatus.OK
    assert result.latency_ms is None
    assert result.error_type == ErrorType.NONE
    assert result.error_detail is None
    assert isinstance(result.timestamp, float)
    assert result.metadata == {}


def test_check_result_model_dump_json_mode_is_json_serializable() -> None:
    result = CheckResult(
        source="vault-prod",
        check_type="auth_api",
        status=CheckStatus.DOWN,
        latency_ms=123.456,
        error_type=ErrorType.HTTP_STATUS,
        error_detail="HTTP 500",
        metadata={"url": "https://example.com", "status_code": 500},
    )

    dumped = result.model_dump(mode="json")

    # Enums must serialize to their plain string values, not repr-like objects.
    assert dumped["status"] == "down"
    assert dumped["error_type"] == "http_status"
    assert isinstance(dumped["latency_ms"], float)
    assert isinstance(dumped["timestamp"], float)

    # round-trips through the standard json module without a custom encoder
    serialized = json.dumps(dumped)
    reloaded = json.loads(serialized)
    assert reloaded["source"] == "vault-prod"
    assert reloaded["metadata"] == {"url": "https://example.com", "status_code": 500}


def test_heartbeat_defaults_and_schema_version() -> None:
    hb = Heartbeat(connectors_configured=5, connectors_ran_last_cycle=5)

    assert hb.schema_version == 1
    assert hb.event_type == "heartbeat"
    assert hb.engine == "healthcheck-engine"
    assert hb.connectors_configured == 5
    assert hb.connectors_ran_last_cycle == 5
    assert isinstance(hb.timestamp, float)


def test_heartbeat_model_dump_json_mode_is_json_serializable() -> None:
    hb = Heartbeat(connectors_configured=3, connectors_ran_last_cycle=2)

    dumped = hb.model_dump(mode="json")
    serialized = json.dumps(dumped)
    reloaded = json.loads(serialized)

    assert reloaded["event_type"] == "heartbeat"
    assert reloaded["connectors_configured"] == 3
    assert reloaded["connectors_ran_last_cycle"] == 2


def test_check_status_enum_values() -> None:
    assert CheckStatus.OK == "ok"
    assert CheckStatus.DEGRADED == "degraded"
    assert CheckStatus.DOWN == "down"
    assert CheckStatus.UNKNOWN == "unknown"
    assert CheckStatus.ERROR == "error"


def test_error_type_enum_values() -> None:
    assert ErrorType.NONE == "none"
    assert ErrorType.TIMEOUT == "timeout"
    assert ErrorType.CONNECTION == "connection"
    assert ErrorType.AUTH == "auth"
    assert ErrorType.HTTP_STATUS == "http_status"
    assert ErrorType.PARSE == "parse"
    assert ErrorType.INTERNAL == "internal"
