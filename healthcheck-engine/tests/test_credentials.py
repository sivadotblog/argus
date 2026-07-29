"""Tests for the SPN (service principal) credential sourcing in credentials.py.

The engine never uses DefaultAzureCredential: all Azure auth goes through an explicit
azure.identity.aio.ClientSecretCredential built from AZURE_TENANT_ID/AZURE_CLIENT_ID/
AZURE_CLIENT_SECRET (or, for connectors, from a Vault-stored SPN). ClientSecretCredential
itself is mocked here — no real Azure calls are made.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from healthcheck_engine.credentials import (
    CredentialManager,
    SpnConfigError,
    client_secret_credential_from_env,
    client_secret_credential_from_vault_secret,
    resolve_spn_credential,
)
from healthcheck_engine.schema import VaultConfig

ENV_VARS = ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")
VAULT_CFG = VaultConfig(addr="https://vault.example:8200", azure_auth_role="healthcheck-engine")


@pytest.fixture(autouse=True)
def clean_spn_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never inherit the developer's real AZURE_* environment."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def set_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    values = {
        "AZURE_TENANT_ID": "tenant-1",
        "AZURE_CLIENT_ID": "client-1",
        "AZURE_CLIENT_SECRET": "secret-1",
    }
    values.update(overrides)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


class FakeCredentialManager:
    def __init__(self, secret: dict[str, Any]) -> None:
        self._secret = secret
        self.calls: list[tuple[str, str]] = []

    async def get_secret(self, path: str, mount: str = "secret") -> dict[str, Any]:
        self.calls.append((path, mount))
        return self._secret


# ---------- client_secret_credential_from_env ----------


def test_from_env_builds_client_secret_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch)
    cls = MagicMock()
    with patch("healthcheck_engine.credentials.ClientSecretCredential", cls):
        credential = client_secret_credential_from_env()

    cls.assert_called_once_with(
        tenant_id="tenant-1", client_id="client-1", client_secret="secret-1"
    )
    assert credential is cls.return_value


def test_from_env_raises_naming_every_missing_env_var() -> None:
    with pytest.raises(SpnConfigError) as excinfo:
        client_secret_credential_from_env()

    message = str(excinfo.value)
    for name in ENV_VARS:
        assert name in message


def test_from_env_raises_naming_only_the_missing_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_env(monkeypatch)
    monkeypatch.delenv("AZURE_CLIENT_SECRET")
    with pytest.raises(SpnConfigError) as excinfo:
        client_secret_credential_from_env()

    message = str(excinfo.value)
    assert "AZURE_CLIENT_SECRET" in message
    assert "AZURE_TENANT_ID" not in message


def test_from_env_treats_empty_string_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch, AZURE_CLIENT_ID="")
    with pytest.raises(SpnConfigError, match="AZURE_CLIENT_ID"):
        client_secret_credential_from_env()


def test_spn_config_error_is_a_runtime_error() -> None:
    assert issubclass(SpnConfigError, RuntimeError)


# ---------- client_secret_credential_from_vault_secret ----------


def test_from_vault_secret_builds_client_secret_credential() -> None:
    cls = MagicMock()
    secret = {"tenant_id": "t", "client_id": "c", "client_secret": "s", "extra": "ignored"}
    with patch("healthcheck_engine.credentials.ClientSecretCredential", cls):
        client_secret_credential_from_vault_secret(secret, "healthchecks/azure-spn")

    cls.assert_called_once_with(tenant_id="t", client_id="c", client_secret="s")


def test_from_vault_secret_raises_spn_config_error_not_key_error() -> None:
    with pytest.raises(SpnConfigError) as excinfo:
        client_secret_credential_from_vault_secret({"tenant_id": "t"}, "healthchecks/spn")

    message = str(excinfo.value)
    assert "healthchecks/spn" in message
    assert "client_id" in message
    assert "client_secret" in message


# ---------- resolve_spn_credential ----------


async def test_resolve_uses_env_spn_when_no_vault_path(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch)
    creds = FakeCredentialManager({"tenant_id": "v", "client_id": "v", "client_secret": "v"})
    cls = MagicMock()
    with patch("healthcheck_engine.credentials.ClientSecretCredential", cls):
        await resolve_spn_credential({}, creds)  # type: ignore[arg-type]

    cls.assert_called_once_with(
        tenant_id="tenant-1", client_id="client-1", client_secret="secret-1"
    )
    assert creds.calls == []  # Vault untouched on the default path


async def test_resolve_uses_vault_spn_when_vault_path_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_env(monkeypatch)
    creds = FakeCredentialManager(
        {"tenant_id": "vt", "client_id": "vc", "client_secret": "vs"}
    )
    cls = MagicMock()
    params = {"vault_path": "healthchecks/azure-spn", "vault_mount": "kv"}
    with patch("healthcheck_engine.credentials.ClientSecretCredential", cls):
        await resolve_spn_credential(params, creds)  # type: ignore[arg-type]

    assert creds.calls == [("healthchecks/azure-spn", "kv")]
    cls.assert_called_once_with(tenant_id="vt", client_id="vc", client_secret="vs")


async def test_resolve_raises_when_vault_path_set_without_vault_config() -> None:
    with pytest.raises(SpnConfigError, match="vault_path configured but no vault config"):
        await resolve_spn_credential({"vault_path": "healthchecks/azure-spn"}, None)


# ---------- CredentialManager startup behaviour ----------


def test_credential_manager_builds_env_spn_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_env(monkeypatch)
    cls = MagicMock()
    with patch("healthcheck_engine.credentials.ClientSecretCredential", cls):
        manager = CredentialManager(VAULT_CFG)

    cls.assert_called_once_with(
        tenant_id="tenant-1", client_id="client-1", client_secret="secret-1"
    )
    assert manager._azure_cred is cls.return_value


async def test_credential_manager_logs_in_to_vault_with_an_spn_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The SPN credential is azure.identity.aio's, so get_token() is awaited on the event
    # loop and only the blocking hvac calls are off-loaded to a thread.
    set_env(monkeypatch)
    credential = MagicMock()
    credential.get_token = AsyncMock(return_value=SimpleNamespace(token="jwt-abc"))
    hvac_client = MagicMock()
    hvac_client.auth.azure.login.return_value = {"auth": {"lease_duration": 3600}}
    hvac_client.secrets.kv.v2.read_secret_version.return_value = {
        "data": {"data": {"username": "u", "password": "p"}}
    }

    with (
        patch("healthcheck_engine.credentials.ClientSecretCredential", return_value=credential),
        patch("healthcheck_engine.credentials.hvac.Client", return_value=hvac_client),
        patch("healthcheck_engine.credentials.emit"),
    ):
        manager = CredentialManager(VAULT_CFG)
        secret = await manager.get_secret("healthchecks/prod-postgres")

    credential.get_token.assert_awaited_once_with("https://management.azure.com/")
    hvac_client.auth.azure.login.assert_called_once_with(
        role="healthcheck-engine", jwt="jwt-abc", mount_point="azure"
    )
    assert secret == {"username": "u", "password": "p"}


def test_credential_manager_fails_fast_when_env_vars_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Constructed once at startup in main(); an incomplete SPN must be a loud, immediate
    # failure rather than a silent per-check auth error.
    set_env(monkeypatch)
    monkeypatch.delenv("AZURE_CLIENT_ID")
    with pytest.raises(SpnConfigError, match="AZURE_CLIENT_ID"):
        CredentialManager(VAULT_CFG)
