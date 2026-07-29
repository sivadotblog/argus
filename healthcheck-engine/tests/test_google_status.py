"""Tests for GoogleStatusConnector, HTTP mocked via respx — no real network calls."""
from __future__ import annotations

import httpx
import respx

from healthcheck_engine.connectors.google_status import (
    GOOGLE_INCIDENTS_URL,
    GoogleStatusConnector,
)
from healthcheck_engine.schema import CheckStatus, ConnectorConfig, ErrorType


def make_cfg(**params: object) -> ConnectorConfig:
    return ConnectorConfig(
        name="gcp-status",
        type="google_status",
        params={"services": ["Google Compute Engine"], **params},
    )


def incident(
    service_name: str = "Google Compute Engine",
    severity: str = "high",
    end: str | None = None,
) -> dict[str, object]:
    return {
        "id": "abc123",
        "number": "20001",
        "begin": "2026-07-27T10:00:00+00:00",
        "end": end,
        "service_name": service_name,
        "severity": severity,
        "external_desc": "We are investigating an issue.",
        "most_recent_update": {"text": "Investigating.", "when": "2026-07-27T10:05:00+00:00"},
    }


@respx.mock
async def test_no_matching_incidents_is_ok() -> None:
    respx.get(GOOGLE_INCIDENTS_URL).mock(
        return_value=httpx.Response(200, json=[incident(service_name="Google Cloud Storage")])
    )
    connector = GoogleStatusConnector(make_cfg(), None)

    result = await connector.check()

    assert result.status == CheckStatus.OK
    assert result.error_type == ErrorType.NONE
    assert result.metadata == {"url": GOOGLE_INCIDENTS_URL}


@respx.mock
async def test_empty_incident_list_is_ok() -> None:
    respx.get(GOOGLE_INCIDENTS_URL).mock(return_value=httpx.Response(200, json=[]))
    connector = GoogleStatusConnector(make_cfg(), None)

    result = await connector.check()

    assert result.status == CheckStatus.OK


@respx.mock
async def test_matching_active_high_severity_incident_is_down() -> None:
    respx.get(GOOGLE_INCIDENTS_URL).mock(
        return_value=httpx.Response(200, json=[incident(severity="high")])
    )
    connector = GoogleStatusConnector(make_cfg(), None)

    result = await connector.check()

    assert result.status == CheckStatus.DOWN
    assert result.error_type == ErrorType.NONE
    assert result.metadata == {
        "url": GOOGLE_INCIDENTS_URL,
        "incident_count": 1,
        "matched_services": ["Google Compute Engine"],
    }


@respx.mock
async def test_matching_active_medium_severity_incident_is_degraded() -> None:
    respx.get(GOOGLE_INCIDENTS_URL).mock(
        return_value=httpx.Response(200, json=[incident(severity="medium")])
    )
    connector = GoogleStatusConnector(make_cfg(), None)

    result = await connector.check()

    assert result.status == CheckStatus.DEGRADED
    assert result.metadata["incident_count"] == 1


@respx.mock
async def test_resolved_incident_ignored_when_active_only() -> None:
    respx.get(GOOGLE_INCIDENTS_URL).mock(
        return_value=httpx.Response(
            200, json=[incident(severity="high", end="2026-07-27T12:00:00+00:00")]
        )
    )
    connector = GoogleStatusConnector(make_cfg(), None)

    result = await connector.check()

    assert result.status == CheckStatus.OK


@respx.mock
async def test_resolved_incident_counts_when_active_only_false() -> None:
    respx.get(GOOGLE_INCIDENTS_URL).mock(
        return_value=httpx.Response(
            200, json=[incident(severity="high", end="2026-07-27T12:00:00+00:00")]
        )
    )
    connector = GoogleStatusConnector(make_cfg(active_only=False), None)

    result = await connector.check()

    assert result.status == CheckStatus.DOWN


@respx.mock
async def test_malformed_json_body_is_unknown_with_parse_error_type() -> None:
    respx.get(GOOGLE_INCIDENTS_URL).mock(
        return_value=httpx.Response(200, content=b"not json at all")
    )
    connector = GoogleStatusConnector(make_cfg(), None)

    result = await connector.check()

    assert result.status == CheckStatus.UNKNOWN
    assert result.error_type == ErrorType.PARSE
    assert result.error_detail


@respx.mock
async def test_empty_body_is_unknown_with_parse_error_type() -> None:
    respx.get(GOOGLE_INCIDENTS_URL).mock(return_value=httpx.Response(200, content=b""))
    connector = GoogleStatusConnector(make_cfg(), None)

    result = await connector.check()

    assert result.status == CheckStatus.UNKNOWN
    assert result.error_type == ErrorType.PARSE


@respx.mock
async def test_unexpected_json_shape_is_unknown_with_parse_error_type() -> None:
    # Valid JSON, but a dict instead of the expected list of incidents.
    respx.get(GOOGLE_INCIDENTS_URL).mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )
    connector = GoogleStatusConnector(make_cfg(), None)

    result = await connector.check()

    assert result.status == CheckStatus.UNKNOWN
    assert result.error_type == ErrorType.PARSE


@respx.mock
async def test_incident_missing_service_name_key_is_unknown_with_parse_error_type() -> None:
    respx.get(GOOGLE_INCIDENTS_URL).mock(
        return_value=httpx.Response(200, json=[{"severity": "high"}])
    )
    connector = GoogleStatusConnector(make_cfg(), None)

    result = await connector.check()

    assert result.status == CheckStatus.UNKNOWN
    assert result.error_type == ErrorType.PARSE


@respx.mock
async def test_timeout_is_down_with_timeout_error_type() -> None:
    respx.get(GOOGLE_INCIDENTS_URL).mock(side_effect=httpx.TimeoutException("timed out"))
    connector = GoogleStatusConnector(make_cfg(), None)

    result = await connector.check()

    assert result.status == CheckStatus.DOWN
    assert result.error_type == ErrorType.TIMEOUT


@respx.mock
async def test_connection_error_is_error_with_connection_error_type() -> None:
    respx.get(GOOGLE_INCIDENTS_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    connector = GoogleStatusConnector(make_cfg(), None)

    result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.CONNECTION


@respx.mock
async def test_substring_service_match() -> None:
    # A config entry with the short "Compute Engine" substring must match a real
    # incident whose service_name is the full "Google Compute Engine" — this is the
    # matching semantic that shipped configs (config/checks.yaml) rely on, and it now
    # mirrors azure_status.py's case-insensitive substring matching against titles.
    respx.get(GOOGLE_INCIDENTS_URL).mock(
        return_value=httpx.Response(
            200, json=[incident(service_name="Google Compute Engine", severity="high")]
        )
    )
    connector = GoogleStatusConnector(make_cfg(services=["Compute Engine"]), None)

    result = await connector.check()

    assert result.status == CheckStatus.DOWN
    assert result.metadata["matched_services"] == ["Google Compute Engine"]


@respx.mock
async def test_case_insensitive_service_match() -> None:
    respx.get(GOOGLE_INCIDENTS_URL).mock(
        return_value=httpx.Response(
            200, json=[incident(service_name="Google Compute Engine", severity="high")]
        )
    )
    connector = GoogleStatusConnector(make_cfg(services=["compute engine"]), None)

    result = await connector.check()

    assert result.status == CheckStatus.DOWN


@respx.mock
async def test_non_matching_substring_is_ok() -> None:
    respx.get(GOOGLE_INCIDENTS_URL).mock(
        return_value=httpx.Response(
            200, json=[incident(service_name="Google Cloud Storage", severity="high")]
        )
    )
    connector = GoogleStatusConnector(make_cfg(services=["Compute Engine"]), None)

    result = await connector.check()

    assert result.status == CheckStatus.OK
