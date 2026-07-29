"""Tests for VnetCapacityConnector: the Azure SDK client and DefaultAzureCredential are
replaced with fakes — no real Azure calls are made.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from azure.core.exceptions import ClientAuthenticationError, ServiceRequestError

from healthcheck_engine.connectors.vnet_capacity import VnetCapacityConnector
from healthcheck_engine.schema import CheckStatus, ConnectorConfig, ErrorType

VNET_ENTRY = {
    "subscription_id": "sub-1",
    "resource_group": "rg-1",
    "vnet_name": "vnet-1",
    "subnet_name": "subnet-1",
}


def make_cfg(**params: object) -> ConnectorConfig:
    return ConnectorConfig(
        name="prod-vnet",
        type="vnet_capacity",
        params={"vnets": [dict(VNET_ENTRY)], **params},
    )


def fake_subnet(used_ips: int, address_prefix: str = "10.0.0.0/24") -> SimpleNamespace:
    # A /24 has 2**8 - 5 = 251 usable IPs per the plan's reservation rule.
    return SimpleNamespace(
        address_prefix=address_prefix,
        ip_configurations=[SimpleNamespace() for _ in range(used_ips)],
    )


class FakeAsyncCM:
    """Minimal async-context-manager stand-in for DefaultAzureCredential/NetworkManagementClient."""

    async def __aenter__(self) -> FakeAsyncCM:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeCredential(FakeAsyncCM):
    pass


class FakeNetworkClient(FakeAsyncCM):
    def __init__(self, subnets_get: AsyncMock) -> None:
        self.subnets = SimpleNamespace(get=subnets_get)


async def test_low_usage_is_ok() -> None:
    subnets_get = AsyncMock(return_value=fake_subnet(used_ips=10))
    connector = VnetCapacityConnector(make_cfg(), None)
    with (
        patch(
            "healthcheck_engine.connectors.vnet_capacity.DefaultAzureCredential",
            return_value=FakeCredential(),
        ),
        patch(
            "healthcheck_engine.connectors.vnet_capacity.NetworkManagementClient",
            return_value=FakeNetworkClient(subnets_get),
        ),
    ):
        result = await connector.check()

    assert result.status == CheckStatus.OK
    assert result.error_type == ErrorType.NONE
    assert result.metadata["subnets"] == [
        {"subnet": "subnet-1", "total": 251, "used": 10, "available": 241, "pct_used": 3.98}
    ]


async def test_usage_at_degraded_threshold_is_degraded() -> None:
    # 210 / 251 = 83.67% >= default degraded_pct (80), < default down_pct (95).
    subnets_get = AsyncMock(return_value=fake_subnet(used_ips=210))
    connector = VnetCapacityConnector(make_cfg(), None)
    with (
        patch(
            "healthcheck_engine.connectors.vnet_capacity.DefaultAzureCredential",
            return_value=FakeCredential(),
        ),
        patch(
            "healthcheck_engine.connectors.vnet_capacity.NetworkManagementClient",
            return_value=FakeNetworkClient(subnets_get),
        ),
    ):
        result = await connector.check()

    assert result.status == CheckStatus.DEGRADED
    assert result.metadata["subnets"][0]["pct_used"] == 83.67


async def test_usage_at_down_threshold_is_down() -> None:
    # 240 / 251 = 95.62% >= default down_pct (95).
    subnets_get = AsyncMock(return_value=fake_subnet(used_ips=240))
    connector = VnetCapacityConnector(make_cfg(), None)
    with (
        patch(
            "healthcheck_engine.connectors.vnet_capacity.DefaultAzureCredential",
            return_value=FakeCredential(),
        ),
        patch(
            "healthcheck_engine.connectors.vnet_capacity.NetworkManagementClient",
            return_value=FakeNetworkClient(subnets_get),
        ),
    ):
        result = await connector.check()

    assert result.status == CheckStatus.DOWN
    assert result.metadata["subnets"][0]["pct_used"] == 95.62


async def test_custom_thresholds_are_honored() -> None:
    subnets_get = AsyncMock(return_value=fake_subnet(used_ips=210))  # 83.67%
    connector = VnetCapacityConnector(
        make_cfg(degraded_pct=90, down_pct=99), None
    )
    with (
        patch(
            "healthcheck_engine.connectors.vnet_capacity.DefaultAzureCredential",
            return_value=FakeCredential(),
        ),
        patch(
            "healthcheck_engine.connectors.vnet_capacity.NetworkManagementClient",
            return_value=FakeNetworkClient(subnets_get),
        ),
    ):
        result = await connector.check()

    assert result.status == CheckStatus.OK  # 83.67% < 90 now


async def test_worst_status_wins_across_multiple_subnets() -> None:
    cfg = make_cfg(
        vnets=[
            dict(VNET_ENTRY, subnet_name="subnet-ok"),
            dict(VNET_ENTRY, subnet_name="subnet-down"),
        ]
    )
    subnets_get = AsyncMock(
        side_effect=[fake_subnet(used_ips=5), fake_subnet(used_ips=240)]
    )
    connector = VnetCapacityConnector(cfg, None)
    with (
        patch(
            "healthcheck_engine.connectors.vnet_capacity.DefaultAzureCredential",
            return_value=FakeCredential(),
        ),
        patch(
            "healthcheck_engine.connectors.vnet_capacity.NetworkManagementClient",
            return_value=FakeNetworkClient(subnets_get),
        ),
    ):
        result = await connector.check()

    assert result.status == CheckStatus.DOWN
    assert [s["subnet"] for s in result.metadata["subnets"]] == ["subnet-ok", "subnet-down"]


async def test_auth_failure_is_error_with_auth_error_type() -> None:
    subnets_get = AsyncMock(side_effect=ClientAuthenticationError("invalid credentials"))
    connector = VnetCapacityConnector(make_cfg(), None)
    with (
        patch(
            "healthcheck_engine.connectors.vnet_capacity.DefaultAzureCredential",
            return_value=FakeCredential(),
        ),
        patch(
            "healthcheck_engine.connectors.vnet_capacity.NetworkManagementClient",
            return_value=FakeNetworkClient(subnets_get),
        ),
    ):
        result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.AUTH
    assert "invalid credentials" in (result.error_detail or "")


async def test_connection_failure_is_error_with_connection_error_type() -> None:
    subnets_get = AsyncMock(side_effect=ServiceRequestError("connection refused"))
    connector = VnetCapacityConnector(make_cfg(), None)
    with (
        patch(
            "healthcheck_engine.connectors.vnet_capacity.DefaultAzureCredential",
            return_value=FakeCredential(),
        ),
        patch(
            "healthcheck_engine.connectors.vnet_capacity.NetworkManagementClient",
            return_value=FakeNetworkClient(subnets_get),
        ),
    ):
        result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.CONNECTION


async def test_unanticipated_sdk_exception_is_error_with_internal_error_type() -> None:
    # An exception outside azure.core's hierarchy — e.g. an unexpected shape bug — must still
    # be caught and mapped, never raised, per the base.py "must not raise" contract.
    subnets_get = AsyncMock(side_effect=RuntimeError("boom"))
    connector = VnetCapacityConnector(make_cfg(), None)
    with (
        patch(
            "healthcheck_engine.connectors.vnet_capacity.DefaultAzureCredential",
            return_value=FakeCredential(),
        ),
        patch(
            "healthcheck_engine.connectors.vnet_capacity.NetworkManagementClient",
            return_value=FakeNetworkClient(subnets_get),
        ),
    ):
        result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.INTERNAL
    assert "boom" in (result.error_detail or "")


async def test_missing_address_prefix_is_error_with_internal_error_type() -> None:
    subnets_get = AsyncMock(
        return_value=SimpleNamespace(address_prefix=None, ip_configurations=[])
    )
    connector = VnetCapacityConnector(make_cfg(), None)
    with (
        patch(
            "healthcheck_engine.connectors.vnet_capacity.DefaultAzureCredential",
            return_value=FakeCredential(),
        ),
        patch(
            "healthcheck_engine.connectors.vnet_capacity.NetworkManagementClient",
            return_value=FakeNetworkClient(subnets_get),
        ),
    ):
        result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.INTERNAL
