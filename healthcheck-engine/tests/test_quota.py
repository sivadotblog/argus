"""Tests for QuotaConnector: the Azure SDK client and the SPN credential
(azure.identity.aio.ClientSecretCredential, always built inside
healthcheck_engine.credentials) are replaced with fakes — no real Azure calls are made.

DefaultAzureCredential is never used by this connector, so nothing here mocks it.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from azure.core.exceptions import ClientAuthenticationError, ServiceRequestError
from azure.mgmt.quota.models import (
    CurrentQuotaLimitBase,
    CurrentUsagesBase,
    LimitObject,
    QuotaProperties,
    UsagesObject,
    UsagesProperties,
)

from healthcheck_engine.connectors.quota import QuotaConnector, _extract_subscription_id
from healthcheck_engine.schema import CheckStatus, ConnectorConfig, ErrorType

SCOPE = (
    "/subscriptions/11111111-1111-1111-1111-111111111111"
    "/providers/Microsoft.Compute/locations/eastus"
)

SPN_ENV = {
    "AZURE_TENANT_ID": "env-tenant",
    "AZURE_CLIENT_ID": "env-client",
    "AZURE_CLIENT_SECRET": "env-secret",
}


@pytest.fixture(autouse=True)
def spn_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared engine SPN env vars are present by default in every test."""
    for name, value in SPN_ENV.items():
        monkeypatch.setenv(name, value)


def make_cfg(**params: object) -> ConnectorConfig:
    return ConnectorConfig(
        name="prod-quota",
        type="quota",
        params={
            "scopes": [{"scope": SCOPE, "resource_name": "standardDSv3Family"}],
            **params,
        },
    )


def usage_result(value: int) -> CurrentUsagesBase:
    return CurrentUsagesBase(properties=UsagesProperties(usages=UsagesObject(value=value)))


def limit_result(value: int) -> CurrentQuotaLimitBase:
    return CurrentQuotaLimitBase(properties=QuotaProperties(limit=LimitObject(value=value)))


class FakeCredential:
    """Stand-in for a built ClientSecretCredential (opaque to the connector)."""


class FakeQuotaClient:
    def __init__(self, usages_get: AsyncMock, quota_get: AsyncMock) -> None:
        self.usages = SimpleNamespace(get=usages_get)
        self.quota = SimpleNamespace(get=quota_get)

    async def __aenter__(self) -> FakeQuotaClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeCredentialManager:
    """Stands in for CredentialManager for the optional vault_path SPN override."""

    def __init__(self, secret: dict[str, Any] | Exception) -> None:
        self._secret = secret
        self.calls: list[tuple[str, str]] = []

    async def get_secret(self, path: str, mount: str = "secret") -> dict[str, Any]:
        self.calls.append((path, mount))
        if isinstance(self._secret, Exception):
            raise self._secret
        return self._secret


@contextlib.contextmanager
def patched(
    usages_get: AsyncMock, quota_get: AsyncMock
) -> Iterator[tuple[MagicMock, MagicMock]]:
    """Patches SPN credential construction and the Azure SDK client.

    ClientSecretCredential is patched where it is *constructed* — in
    healthcheck_engine.credentials — which covers both the env-var default path and the
    vault_path override path, so the returned spy counts every credential build.
    """
    credential_cls = MagicMock(return_value=FakeCredential())
    quota_cls = MagicMock(return_value=FakeQuotaClient(usages_get, quota_get))
    with (
        patch("healthcheck_engine.credentials.ClientSecretCredential", credential_cls),
        patch("healthcheck_engine.connectors.quota.QuotaMgmtClient", quota_cls),
    ):
        yield credential_cls, quota_cls


def test_extract_subscription_id_parses_scope() -> None:
    assert _extract_subscription_id(SCOPE) == "11111111-1111-1111-1111-111111111111"


def test_extract_subscription_id_raises_for_malformed_scope() -> None:
    with pytest.raises(ValueError, match="could not extract"):
        _extract_subscription_id("not-a-valid-scope")


async def test_low_usage_is_ok() -> None:
    usages_get = AsyncMock(return_value=usage_result(10))
    quota_get = AsyncMock(return_value=limit_result(100))
    connector = QuotaConnector(make_cfg(), None)
    with patched(usages_get, quota_get):
        result = await connector.check()

    assert result.status == CheckStatus.OK
    assert result.error_type == ErrorType.NONE
    assert result.metadata["scopes"] == [
        {
            "scope": SCOPE,
            "resource_name": "standardDSv3Family",
            "usage": 10,
            "limit": 100,
            "pct_used": 10.0,
        }
    ]


async def test_usage_at_degraded_threshold_is_degraded() -> None:
    usages_get = AsyncMock(return_value=usage_result(85))
    quota_get = AsyncMock(return_value=limit_result(100))
    connector = QuotaConnector(make_cfg(), None)
    with patched(usages_get, quota_get):
        result = await connector.check()

    assert result.status == CheckStatus.DEGRADED
    assert result.metadata["scopes"][0]["pct_used"] == 85.0


async def test_usage_at_down_threshold_is_down() -> None:
    usages_get = AsyncMock(return_value=usage_result(96))
    quota_get = AsyncMock(return_value=limit_result(100))
    connector = QuotaConnector(make_cfg(), None)
    with patched(usages_get, quota_get):
        result = await connector.check()

    assert result.status == CheckStatus.DOWN
    assert result.metadata["scopes"][0]["pct_used"] == 96.0


async def test_custom_thresholds_are_honored() -> None:
    usages_get = AsyncMock(return_value=usage_result(85))
    quota_get = AsyncMock(return_value=limit_result(100))
    connector = QuotaConnector(make_cfg(degraded_pct=90, down_pct=99), None)
    with patched(usages_get, quota_get):
        result = await connector.check()

    assert result.status == CheckStatus.OK  # 85% < 90 now


async def test_worst_status_wins_across_multiple_scopes() -> None:
    other_scope = (
        "/subscriptions/22222222-2222-2222-2222-222222222222"
        "/providers/Microsoft.Compute/locations/westus"
    )
    cfg = make_cfg(
        scopes=[
            {"scope": SCOPE, "resource_name": "standardDSv3Family"},
            {"scope": other_scope, "resource_name": "standardDSv3Family"},
        ]
    )
    usages_get = AsyncMock(side_effect=[usage_result(5), usage_result(96)])
    quota_get = AsyncMock(side_effect=[limit_result(100), limit_result(100)])
    connector = QuotaConnector(cfg, None)
    with patched(usages_get, quota_get):
        result = await connector.check()

    assert result.status == CheckStatus.DOWN
    assert [s["scope"] for s in result.metadata["scopes"]] == [SCOPE, other_scope]


# ---------- SPN credential sourcing ----------


async def test_default_path_builds_spn_from_env_vars_and_passes_it_to_the_sdk() -> None:
    usages_get = AsyncMock(return_value=usage_result(10))
    quota_get = AsyncMock(return_value=limit_result(100))
    connector = QuotaConnector(make_cfg(), None)  # no vault_path param
    with patched(usages_get, quota_get) as (credential_cls, quota_cls):
        result = await connector.check()

    assert result.status == CheckStatus.OK
    credential_cls.assert_called_once_with(
        tenant_id="env-tenant", client_id="env-client", client_secret="env-secret"
    )
    assert quota_cls.call_args.args[0] is credential_cls.return_value
    assert quota_cls.call_args.args[1] == "11111111-1111-1111-1111-111111111111"


async def test_missing_env_vars_is_error_with_auth_error_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AZURE_TENANT_ID")
    usages_get = AsyncMock(return_value=usage_result(10))
    quota_get = AsyncMock(return_value=limit_result(100))
    connector = QuotaConnector(make_cfg(), None)
    with patched(usages_get, quota_get) as (credential_cls, _):
        result = await connector.check()  # must not raise

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.AUTH
    assert "AZURE_TENANT_ID" in (result.error_detail or "")
    credential_cls.assert_not_called()
    usages_get.assert_not_awaited()


async def test_vault_path_sources_a_distinct_spn_from_vault() -> None:
    creds = FakeCredentialManager(
        {
            "tenant_id": "vault-tenant",
            "client_id": "vault-client",
            "client_secret": "vault-secret",
        }
    )
    usages_get = AsyncMock(return_value=usage_result(10))
    quota_get = AsyncMock(return_value=limit_result(100))
    cfg = make_cfg(vault_path="healthchecks/azure-spn", vault_mount="kv")
    connector = QuotaConnector(cfg, creds)  # type: ignore[arg-type]
    with patched(usages_get, quota_get) as (credential_cls, _):
        result = await connector.check()

    assert result.status == CheckStatus.OK
    assert creds.calls == [("healthchecks/azure-spn", "kv")]
    credential_cls.assert_called_once_with(
        tenant_id="vault-tenant", client_id="vault-client", client_secret="vault-secret"
    )


async def test_vault_path_without_vault_config_is_error_with_auth_error_type() -> None:
    usages_get = AsyncMock(return_value=usage_result(10))
    quota_get = AsyncMock(return_value=limit_result(100))
    connector = QuotaConnector(make_cfg(vault_path="healthchecks/azure-spn"), None)
    with patched(usages_get, quota_get) as (credential_cls, _):
        result = await connector.check()  # must not raise

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.AUTH
    assert result.error_detail == "vault_path configured but no vault config present"
    credential_cls.assert_not_called()


async def test_vault_read_failure_is_error_with_auth_error_type() -> None:
    creds = FakeCredentialManager(RuntimeError("vault sealed"))
    usages_get = AsyncMock(return_value=usage_result(10))
    quota_get = AsyncMock(return_value=limit_result(100))
    connector = QuotaConnector(
        make_cfg(vault_path="healthchecks/azure-spn"), creds  # type: ignore[arg-type]
    )
    with patched(usages_get, quota_get):
        result = await connector.check()  # must not raise

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.AUTH
    assert "vault sealed" in (result.error_detail or "")


async def test_vault_secret_missing_keys_is_error_with_auth_error_type() -> None:
    creds = FakeCredentialManager({"tenant_id": "t", "client_secret": "s"})  # no client_id
    usages_get = AsyncMock(return_value=usage_result(10))
    quota_get = AsyncMock(return_value=limit_result(100))
    connector = QuotaConnector(
        make_cfg(vault_path="healthchecks/azure-spn"), creds  # type: ignore[arg-type]
    )
    with patched(usages_get, quota_get):
        result = await connector.check()  # must not raise (no bare KeyError)

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.AUTH
    assert "client_id" in (result.error_detail or "")


async def test_credential_is_built_once_and_reused_across_checks() -> None:
    usages_get = AsyncMock(return_value=usage_result(10))
    quota_get = AsyncMock(return_value=limit_result(100))
    connector = QuotaConnector(make_cfg(), None)
    with patched(usages_get, quota_get) as (credential_cls, quota_cls):
        first = await connector.check()
        second = await connector.check()

    assert (first.status, second.status) == (CheckStatus.OK, CheckStatus.OK)
    assert credential_cls.call_count == 1  # not rebuilt per check
    assert quota_cls.call_count == 2  # but the SDK client still is


# ---------- fail-soft mapping ----------


async def test_auth_failure_is_error_with_auth_error_type() -> None:
    usages_get = AsyncMock(side_effect=ClientAuthenticationError("invalid credentials"))
    quota_get = AsyncMock(return_value=limit_result(100))
    connector = QuotaConnector(make_cfg(), None)
    with patched(usages_get, quota_get):
        result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.AUTH
    assert "invalid credentials" in (result.error_detail or "")


async def test_connection_failure_is_error_with_connection_error_type() -> None:
    usages_get = AsyncMock(side_effect=ServiceRequestError("connection refused"))
    quota_get = AsyncMock(return_value=limit_result(100))
    connector = QuotaConnector(make_cfg(), None)
    with patched(usages_get, quota_get):
        result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.CONNECTION


async def test_unanticipated_sdk_exception_is_error_with_internal_error_type() -> None:
    usages_get = AsyncMock(side_effect=RuntimeError("boom"))
    quota_get = AsyncMock(return_value=limit_result(100))
    connector = QuotaConnector(make_cfg(), None)
    with patched(usages_get, quota_get):
        result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.INTERNAL
    assert "boom" in (result.error_detail or "")


async def test_malformed_scope_is_error_with_internal_error_type() -> None:
    cfg = make_cfg(scopes=[{"scope": "not-a-scope", "resource_name": "standardDSv3Family"}])
    usages_get = AsyncMock(return_value=usage_result(10))
    quota_get = AsyncMock(return_value=limit_result(100))
    connector = QuotaConnector(cfg, None)
    with patched(usages_get, quota_get):
        result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.INTERNAL
