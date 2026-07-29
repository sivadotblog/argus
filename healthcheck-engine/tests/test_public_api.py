"""Tests for PublicApiConnector, HTTP mocked via respx — no real network calls."""
from __future__ import annotations

import httpx
import respx

from healthcheck_engine.connectors.public_api import PublicApiConnector
from healthcheck_engine.schema import CheckStatus, ConnectorConfig, ErrorType

URL = "https://status.example.com/health"


def make_cfg(**params: object) -> ConnectorConfig:
    return ConnectorConfig(
        name="example-status",
        type="public_api",
        params={"url": URL, **params},
    )


@respx.mock
async def test_200_fast_response_is_ok() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200))
    connector = PublicApiConnector(make_cfg(), None)

    result = await connector.check()

    assert result.status == CheckStatus.OK
    assert result.error_type == ErrorType.NONE
    assert result.latency_ms is not None
    assert result.metadata == {"url": URL, "status_code": 200}


@respx.mock
async def test_200_response_over_degraded_threshold_is_degraded() -> None:
    # A mocked response always takes >0ms of wall-clock time to process. Rather than
    # sleeping to simulate real latency (flaky under load), we set degraded_latency_ms
    # below any achievable latency so a normal fast mocked response reliably crosses it.
    respx.get(URL).mock(return_value=httpx.Response(200))
    connector = PublicApiConnector(make_cfg(degraded_latency_ms=-1), None)

    result = await connector.check()

    assert result.status == CheckStatus.DEGRADED
    assert result.error_type == ErrorType.NONE
    assert result.latency_ms is not None


@respx.mock
async def test_unexpected_status_code_is_down() -> None:
    respx.get(URL).mock(return_value=httpx.Response(500))
    connector = PublicApiConnector(make_cfg(), None)

    result = await connector.check()

    assert result.status == CheckStatus.DOWN
    assert result.error_type == ErrorType.HTTP_STATUS
    assert result.error_detail == "HTTP 500"
    assert result.metadata == {"url": URL, "status_code": 500}


@respx.mock
async def test_timeout_is_down_with_timeout_error_type() -> None:
    respx.get(URL).mock(side_effect=httpx.TimeoutException("timed out"))
    connector = PublicApiConnector(make_cfg(), None)

    result = await connector.check()

    assert result.status == CheckStatus.DOWN
    assert result.error_type == ErrorType.TIMEOUT
    assert result.metadata == {"url": URL}


@respx.mock
async def test_connection_error_is_error_with_connection_error_type() -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectError("connection refused"))
    connector = PublicApiConnector(make_cfg(), None)

    result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.CONNECTION
    assert result.error_detail == "connection refused"
    assert result.metadata == {"url": URL}
