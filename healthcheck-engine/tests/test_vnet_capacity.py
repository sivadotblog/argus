"""Tests for VnetCapacityConnector: the Azure SDK client and the SPN credential
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

from healthcheck_engine.connectors.vnet_capacity import VnetCapacityConnector
from healthcheck_engine.schema import CheckStatus, ConnectorConfig, ErrorType

VNET_ENTRY = {
    "subscription_id": "sub-1",
    "resource_group": "rg-1",
    "vnet_name": "vnet-1",
    "subnet_name": "subnet-1",
}

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


class FakeCredential:
    """Stand-in for a built ClientSecretCredential (opaque to the connector)."""


class FakeNetworkClient:
    def __init__(self, subnets_get: AsyncMock) -> None:
        self.subnets = SimpleNamespace(get=subnets_get)

    async def __aenter__(self) -> FakeNetworkClient:
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
def patched(subnets_get: AsyncMock) -> Iterator[tuple[MagicMock, MagicMock]]:
    """Patches SPN credential construction and the Azure SDK client.

    ClientSecretCredential is patched where it is *constructed* — in
    healthcheck_engine.credentials — which covers both the env-var default path and the
    vault_path override path, so the returned spy counts every credential build.
    """
    credential_cls = MagicMock(return_value=FakeCredential())
    network_cls = MagicMock(return_value=FakeNetworkClient(subnets_get))
    with (
        patch("healthcheck_engine.credentials.ClientSecretCredential", credential_cls),
        patch(
            "healthcheck_engine.connectors.vnet_capacity.NetworkManagementClient",
            network_cls,
        ),
    ):
        yield credential_cls, network_cls


async def test_low_usage_is_ok() -> None:
    subnets_get = AsyncMock(return_value=fake_subnet(used_ips=10))
    connector = VnetCapacityConnector(make_cfg(), None)
    with patched(subnets_get):
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
    with patched(subnets_get):
        result = await connector.check()

    assert result.status == CheckStatus.DEGRADED
    assert result.metadata["subnets"][0]["pct_used"] == 83.67


async def test_usage_at_down_threshold_is_down() -> None:
    # 240 / 251 = 95.62% >= default down_pct (95).
    subnets_get = AsyncMock(return_value=fake_subnet(used_ips=240))
    connector = VnetCapacityConnector(make_cfg(), None)
    with patched(subnets_get):
        result = await connector.check()

    assert result.status == CheckStatus.DOWN
    assert result.metadata["subnets"][0]["pct_used"] == 95.62


async def test_custom_thresholds_are_honored() -> None:
    subnets_get = AsyncMock(return_value=fake_subnet(used_ips=210))  # 83.67%
    connector = VnetCapacityConnector(make_cfg(degraded_pct=90, down_pct=99), None)
    with patched(subnets_get):
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
    with patched(subnets_get):
        result = await connector.check()

    assert result.status == CheckStatus.DOWN
    assert [s["subnet"] for s in result.metadata["subnets"]] == ["subnet-ok", "subnet-down"]


# ---------- SPN credential sourcing ----------


async def test_default_path_builds_spn_from_env_vars_and_passes_it_to_the_sdk() -> None:
    subnets_get = AsyncMock(return_value=fake_subnet(used_ips=10))
    connector = VnetCapacityConnector(make_cfg(), None)  # no vault_path param
    with patched(subnets_get) as (credential_cls, network_cls):
        result = await connector.check()

    assert result.status == CheckStatus.OK
    credential_cls.assert_called_once_with(
        tenant_id="env-tenant", client_id="env-client", client_secret="env-secret"
    )
    # The SDK client was handed exactly the credential that was built.
    assert network_cls.call_args.args[0] is credential_cls.return_value
    assert network_cls.call_args.args[1] == "sub-1"


async def test_missing_env_vars_is_error_with_auth_error_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AZURE_CLIENT_SECRET")
    subnets_get = AsyncMock(return_value=fake_subnet(used_ips=10))
    connector = VnetCapacityConnector(make_cfg(), None)
    with patched(subnets_get) as (credential_cls, _):
        result = await connector.check()  # must not raise

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.AUTH
    assert "AZURE_CLIENT_SECRET" in (result.error_detail or "")
    credential_cls.assert_not_called()
    subnets_get.assert_not_awaited()


async def test_vault_path_sources_a_distinct_spn_from_vault() -> None:
    creds = FakeCredentialManager(
        {
            "tenant_id": "vault-tenant",
            "client_id": "vault-client",
            "client_secret": "vault-secret",
        }
    )
    subnets_get = AsyncMock(return_value=fake_subnet(used_ips=10))
    cfg = make_cfg(vault_path="healthchecks/azure-spn", vault_mount="kv")
    connector = VnetCapacityConnector(cfg, creds)  # type: ignore[arg-type]
    with patched(subnets_get) as (credential_cls, _):
        result = await connector.check()

    assert result.status == CheckStatus.OK
    assert creds.calls == [("healthchecks/azure-spn", "kv")]
    credential_cls.assert_called_once_with(
        tenant_id="vault-tenant", client_id="vault-client", client_secret="vault-secret"
    )


async def test_vault_path_defaults_to_secret_mount() -> None:
    creds = FakeCredentialManager(
        {"tenant_id": "t", "client_id": "c", "client_secret": "s"}
    )
    subnets_get = AsyncMock(return_value=fake_subnet(used_ips=10))
    connector = VnetCapacityConnector(
        make_cfg(vault_path="healthchecks/azure-spn"), creds  # type: ignore[arg-type]
    )
    with patched(subnets_get):
        await connector.check()

    assert creds.calls == [("healthchecks/azure-spn", "secret")]


async def test_vault_path_without_vault_config_is_error_with_auth_error_type() -> None:
    subnets_get = AsyncMock(return_value=fake_subnet(used_ips=10))
    connector = VnetCapacityConnector(make_cfg(vault_path="healthchecks/azure-spn"), None)
    with patched(subnets_get) as (credential_cls, _):
        result = await connector.check()  # must not raise

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.AUTH
    assert result.error_detail == "vault_path configured but no vault config present"
    credential_cls.assert_not_called()


async def test_vault_read_failure_is_error_with_auth_error_type() -> None:
    creds = FakeCredentialManager(RuntimeError("vault sealed"))
    subnets_get = AsyncMock(return_value=fake_subnet(used_ips=10))
    connector = VnetCapacityConnector(
        make_cfg(vault_path="healthchecks/azure-spn"), creds  # type: ignore[arg-type]
    )
    with patched(subnets_get):
        result = await connector.check()  # must not raise

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.AUTH
    assert "vault sealed" in (result.error_detail or "")


async def test_vault_secret_missing_keys_is_error_with_auth_error_type() -> None:
    creds = FakeCredentialManager({"tenant_id": "t", "client_id": "c"})  # no client_secret
    subnets_get = AsyncMock(return_value=fake_subnet(used_ips=10))
    connector = VnetCapacityConnector(
        make_cfg(vault_path="healthchecks/azure-spn"), creds  # type: ignore[arg-type]
    )
    with patched(subnets_get):
        result = await connector.check()  # must not raise (no bare KeyError)

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.AUTH
    assert "client_secret" in (result.error_detail or "")


async def test_credential_is_built_once_and_reused_across_checks() -> None:
    subnets_get = AsyncMock(return_value=fake_subnet(used_ips=10))
    connector = VnetCapacityConnector(make_cfg(), None)
    with patched(subnets_get) as (credential_cls, network_cls):
        first = await connector.check()
        second = await connector.check()
        third = await connector.check()

    assert (first.status, second.status, third.status) == (
        CheckStatus.OK,
        CheckStatus.OK,
        CheckStatus.OK,
    )
    assert credential_cls.call_count == 1  # not rebuilt per check
    assert network_cls.call_count == 3  # but the SDK client still is


async def test_vault_sourced_credential_is_built_once_across_checks() -> None:
    creds = FakeCredentialManager(
        {"tenant_id": "t", "client_id": "c", "client_secret": "s"}
    )
    subnets_get = AsyncMock(return_value=fake_subnet(used_ips=10))
    connector = VnetCapacityConnector(
        make_cfg(vault_path="healthchecks/azure-spn"), creds  # type: ignore[arg-type]
    )
    with patched(subnets_get) as (credential_cls, _):
        await connector.check()
        await connector.check()

    assert credential_cls.call_count == 1
    assert len(creds.calls) == 1  # Vault is not re-read on every check either


# ---------- fail-soft mapping ----------


async def test_auth_failure_is_error_with_auth_error_type() -> None:
    subnets_get = AsyncMock(side_effect=ClientAuthenticationError("invalid credentials"))
    connector = VnetCapacityConnector(make_cfg(), None)
    with patched(subnets_get):
        result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.AUTH
    assert "invalid credentials" in (result.error_detail or "")


async def test_connection_failure_is_error_with_connection_error_type() -> None:
    subnets_get = AsyncMock(side_effect=ServiceRequestError("connection refused"))
    connector = VnetCapacityConnector(make_cfg(), None)
    with patched(subnets_get):
        result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.CONNECTION


async def test_unanticipated_sdk_exception_is_error_with_internal_error_type() -> None:
    # An exception outside azure.core's hierarchy — e.g. an unexpected shape bug — must still
    # be caught and mapped, never raised, per the base.py "must not raise" contract.
    subnets_get = AsyncMock(side_effect=RuntimeError("boom"))
    connector = VnetCapacityConnector(make_cfg(), None)
    with patched(subnets_get):
        result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.INTERNAL
    assert "boom" in (result.error_detail or "")


async def test_missing_address_prefix_is_error_with_internal_error_type() -> None:
    subnets_get = AsyncMock(
        return_value=SimpleNamespace(address_prefix=None, ip_configurations=[])
    )
    connector = VnetCapacityConnector(make_cfg(), None)
    with patched(subnets_get):
        result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.INTERNAL
