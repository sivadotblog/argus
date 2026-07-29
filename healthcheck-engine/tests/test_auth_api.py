"""Tests for AuthApiConnector: HTTP mocked via respx, Vault mocked via a fake
CredentialManager — no real network calls, no real Vault.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from healthcheck_engine.connectors.auth_api import AuthApiConnector
from healthcheck_engine.schema import CheckStatus, ConnectorConfig, ErrorType

URL = "https://api.example.com/v1/ping"


class FakeCredentialManager:
    """Stands in for credentials.CredentialManager: async get_secret(path, mount)."""

    def __init__(self, secret: dict[str, str]) -> None:
        self._secret = secret
        self.calls: list[tuple[str, str]] = []

    async def get_secret(self, path: str, mount: str = "secret") -> dict[str, Any]:
        self.calls.append((path, mount))
        return self._secret


class FailingCredentialManager:
    """Simulates a Vault outage/auth failure during secret retrieval."""

    async def get_secret(self, path: str, mount: str = "secret") -> dict[str, Any]:
        raise RuntimeError("vault unreachable")


def make_cfg(**params: object) -> ConnectorConfig:
    return ConnectorConfig(
        name="example-auth-api",
        type="auth_api",
        params={"url": URL, "vault_path": "healthchecks/example-api", **params},
    )


@respx.mock
async def test_200_bearer_auth_is_ok_and_sends_bearer_header() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200))
    creds = FakeCredentialManager({"token": "fake-token"})
    connector = AuthApiConnector(make_cfg(), creds)  # type: ignore[arg-type]

    result = await connector.check()

    assert result.status == CheckStatus.OK
    assert result.error_type == ErrorType.NONE
    assert route.calls.last.request.headers["Authorization"] == "Bearer fake-token"


@pytest.mark.parametrize("status_code", [401, 403])
@respx.mock
async def test_401_or_403_is_error_with_auth_error_type(status_code: int) -> None:
    respx.get(URL).mock(return_value=httpx.Response(status_code))
    creds = FakeCredentialManager({"token": "fake-token"})
    connector = AuthApiConnector(make_cfg(), creds)  # type: ignore[arg-type]

    result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.AUTH
    assert result.metadata == {"url": URL, "status_code": status_code}


@respx.mock
async def test_unexpected_non_2xx_status_is_down_with_http_status_error_type() -> None:
    respx.get(URL).mock(return_value=httpx.Response(500))
    creds = FakeCredentialManager({"token": "fake-token"})
    connector = AuthApiConnector(make_cfg(), creds)  # type: ignore[arg-type]

    result = await connector.check()

    assert result.status == CheckStatus.DOWN
    assert result.error_type == ErrorType.HTTP_STATUS
    assert result.metadata == {"url": URL, "status_code": 500}


@respx.mock
async def test_timeout_is_down_with_timeout_error_type() -> None:
    respx.get(URL).mock(side_effect=httpx.TimeoutException("timed out"))
    creds = FakeCredentialManager({"token": "fake-token"})
    connector = AuthApiConnector(make_cfg(), creds)  # type: ignore[arg-type]

    result = await connector.check()

    assert result.status == CheckStatus.DOWN
    assert result.error_type == ErrorType.TIMEOUT


async def test_vault_secret_failure_is_error_with_auth_error_type() -> None:
    connector = AuthApiConnector(make_cfg(), FailingCredentialManager())  # type: ignore[arg-type]

    result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.AUTH
    assert "vault unreachable" in (result.error_detail or "")
