"""Tests for QuotaConnector: the Azure SDK client and DefaultAzureCredential are replaced with
fakes — no real Azure calls are made.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

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


class FakeAsyncCM:
    """Minimal async-context-manager stand-in for DefaultAzureCredential/QuotaMgmtClient."""

    async def __aenter__(self) -> FakeAsyncCM:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeCredential(FakeAsyncCM):
    pass


class FakeQuotaClient(FakeAsyncCM):
    def __init__(self, usages_get: AsyncMock, quota_get: AsyncMock) -> None:
        self.usages = SimpleNamespace(get=usages_get)
        self.quota = SimpleNamespace(get=quota_get)


def patched(usages_get: AsyncMock, quota_get: AsyncMock) -> tuple[Any, Any]:
    client = FakeQuotaClient(usages_get, quota_get)
    return (
        patch(
            "healthcheck_engine.connectors.quota.DefaultAzureCredential",
            return_value=FakeCredential(),
        ),
        patch(
            "healthcheck_engine.connectors.quota.QuotaMgmtClient",
            return_value=client,
        ),
    )


def test_extract_subscription_id_parses_scope() -> None:
    assert _extract_subscription_id(SCOPE) == "11111111-1111-1111-1111-111111111111"


def test_extract_subscription_id_raises_for_malformed_scope() -> None:
    with pytest.raises(ValueError, match="could not extract"):
        _extract_subscription_id("not-a-valid-scope")


async def test_low_usage_is_ok() -> None:
    usages_get = AsyncMock(return_value=usage_result(10))
    quota_get = AsyncMock(return_value=limit_result(100))
    connector = QuotaConnector(make_cfg(), None)
    cred_patch, client_patch = patched(usages_get, quota_get)
    with cred_patch, client_patch:
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
    cred_patch, client_patch = patched(usages_get, quota_get)
    with cred_patch, client_patch:
        result = await connector.check()

    assert result.status == CheckStatus.DEGRADED
    assert result.metadata["scopes"][0]["pct_used"] == 85.0


async def test_usage_at_down_threshold_is_down() -> None:
    usages_get = AsyncMock(return_value=usage_result(96))
    quota_get = AsyncMock(return_value=limit_result(100))
    connector = QuotaConnector(make_cfg(), None)
    cred_patch, client_patch = patched(usages_get, quota_get)
    with cred_patch, client_patch:
        result = await connector.check()

    assert result.status == CheckStatus.DOWN
    assert result.metadata["scopes"][0]["pct_used"] == 96.0


async def test_custom_thresholds_are_honored() -> None:
    usages_get = AsyncMock(return_value=usage_result(85))
    quota_get = AsyncMock(return_value=limit_result(100))
    connector = QuotaConnector(make_cfg(degraded_pct=90, down_pct=99), None)
    cred_patch, client_patch = patched(usages_get, quota_get)
    with cred_patch, client_patch:
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
    cred_patch, client_patch = patched(usages_get, quota_get)
    with cred_patch, client_patch:
        result = await connector.check()

    assert result.status == CheckStatus.DOWN
    assert [s["scope"] for s in result.metadata["scopes"]] == [SCOPE, other_scope]


async def test_auth_failure_is_error_with_auth_error_type() -> None:
    usages_get = AsyncMock(side_effect=ClientAuthenticationError("invalid credentials"))
    quota_get = AsyncMock(return_value=limit_result(100))
    connector = QuotaConnector(make_cfg(), None)
    cred_patch, client_patch = patched(usages_get, quota_get)
    with cred_patch, client_patch:
        result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.AUTH
    assert "invalid credentials" in (result.error_detail or "")


async def test_connection_failure_is_error_with_connection_error_type() -> None:
    usages_get = AsyncMock(side_effect=ServiceRequestError("connection refused"))
    quota_get = AsyncMock(return_value=limit_result(100))
    connector = QuotaConnector(make_cfg(), None)
    cred_patch, client_patch = patched(usages_get, quota_get)
    with cred_patch, client_patch:
        result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.CONNECTION


async def test_unanticipated_sdk_exception_is_error_with_internal_error_type() -> None:
    usages_get = AsyncMock(side_effect=RuntimeError("boom"))
    quota_get = AsyncMock(return_value=limit_result(100))
    connector = QuotaConnector(make_cfg(), None)
    cred_patch, client_patch = patched(usages_get, quota_get)
    with cred_patch, client_patch:
        result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.INTERNAL
    assert "boom" in (result.error_detail or "")


async def test_malformed_scope_is_error_with_internal_error_type() -> None:
    cfg = make_cfg(scopes=[{"scope": "not-a-scope", "resource_name": "standardDSv3Family"}])
    usages_get = AsyncMock(return_value=usage_result(10))
    quota_get = AsyncMock(return_value=limit_result(100))
    connector = QuotaConnector(cfg, None)
    cred_patch, client_patch = patched(usages_get, quota_get)
    with cred_patch, client_patch:
        result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.INTERNAL
