"""Azure SPN (service principal) → Vault (azure auth method) → service secrets.

Design:
- Azure authentication is ALWAYS explicit service-principal authentication via
  azure.identity.aio.ClientSecretCredential. DefaultAzureCredential is deliberately
  never used anywhere in this codebase: its cascading resolution (env → workload
  identity → managed identity → Azure CLI → ...) is unpredictable inside a container
  and can silently fall back to a different identity than intended, which is not
  acceptable for a centralized production health-check engine.
- The SPN comes from the standard Azure SDK environment variables AZURE_TENANT_ID /
  AZURE_CLIENT_ID / AZURE_CLIENT_SECRET (see client_secret_credential_from_env).
- Login to Vault once, cache the client, renew before TTL expiry.
- Per-secret cache with short TTL so every check doesn't hit Vault.
- Emits an `engine_auth` CheckResult so Splunk can alert on OUR auth breaking —
  the classic silent failure mode where everything 403s after N hours.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping
from typing import Any

import hvac
from azure.identity.aio import ClientSecretCredential

from .emitter import emit
from .schema import CheckResult, CheckStatus, ErrorType, VaultConfig

_VAULT_RESOURCE = "https://management.azure.com/"
_SECRET_CACHE_TTL = 300  # seconds
_RENEW_SAFETY_MARGIN = 120  # renew this many seconds before token TTL expires

# The three pieces of SPN material, and the environment variable each is read from by
# client_secret_credential_from_env(). Vault-sourced SPNs use the keys themselves.
_SPN_KEYS = ("tenant_id", "client_id", "client_secret")
_SPN_ENV_VARS: Mapping[str, str] = {
    "tenant_id": "AZURE_TENANT_ID",
    "client_id": "AZURE_CLIENT_ID",
    "client_secret": "AZURE_CLIENT_SECRET",
}


class SpnConfigError(RuntimeError):
    """SPN credential material is missing or incomplete.

    Raised at process start by CredentialManager.__init__ (where it is intended to be a
    loud, immediate startup failure) and, for connectors, mapped to a
    status=ERROR/error_type=AUTH CheckResult so check() never raises.
    """


def _credential_from_values(
    values: Mapping[str, Any], *, names: Mapping[str, str], source: str
) -> ClientSecretCredential:
    """Build a ClientSecretCredential, raising SpnConfigError naming what's missing.

    `names` maps each _SPN_KEYS entry to the label used in the error message (the env
    var name, or the Vault secret key), so failures point at what the operator must fix.
    """
    missing = [names[key] for key in _SPN_KEYS if not values.get(key)]
    if missing:
        raise SpnConfigError(f"{source}: missing or empty {', '.join(missing)}")
    return ClientSecretCredential(
        tenant_id=str(values["tenant_id"]),
        client_id=str(values["client_id"]),
        client_secret=str(values["client_secret"]),
    )


def client_secret_credential_from_env() -> ClientSecretCredential:
    """The shared engine SPN, from AZURE_TENANT_ID/AZURE_CLIENT_ID/AZURE_CLIENT_SECRET.

    Raises SpnConfigError (a RuntimeError) if any of the three is unset or empty.
    """
    values = {key: os.environ.get(env_var, "") for key, env_var in _SPN_ENV_VARS.items()}
    return _credential_from_values(
        values, names=_SPN_ENV_VARS, source="SPN environment variables"
    )


def client_secret_credential_from_vault_secret(
    secret: Mapping[str, Any], vault_path: str
) -> ClientSecretCredential:
    """A distinct SPN read from a Vault secret with tenant_id/client_id/client_secret keys.

    Raises SpnConfigError (never a bare KeyError) if any expected key is missing/empty.
    """
    return _credential_from_values(
        secret,
        names={key: key for key in _SPN_KEYS},
        source=f"vault secret {vault_path!r}",
    )


async def resolve_spn_credential(
    params: Mapping[str, Any], creds: CredentialManager | None
) -> ClientSecretCredential:
    """Resolve the SPN an Azure connector should authenticate with.

    Default: the shared engine SPN from the environment. If the connector's params carry
    the optional `vault_path`, a distinct SPN is read from Vault at that path instead
    (keys: tenant_id/client_id/client_secret; mount from `vault_mount`, default "secret").

    Every failure mode raises — callers (connectors) map that to ERROR/AUTH so that
    check() honours the "must never raise" contract in connectors/base.py.
    """
    vault_path = params.get("vault_path")
    if vault_path is None:
        return client_secret_credential_from_env()
    if creds is None:
        raise SpnConfigError("vault_path configured but no vault config present")
    secret = await creds.get_secret(vault_path, params.get("vault_mount", "secret"))
    return client_secret_credential_from_vault_secret(secret, vault_path)


class CredentialManager:
    def __init__(self, cfg: VaultConfig) -> None:
        self._cfg = cfg
        # Constructed once at process startup (main() builds one CredentialManager when
        # cfg.vault is set), so an incomplete SPN must fail fast and loudly here rather
        # than degrade into a per-check auth error. Unlike Connector.check(), __init__ is
        # allowed — and intended — to raise.
        self._azure_cred = client_secret_credential_from_env()
        self._client: hvac.Client | None = None
        self._token_expiry: float = 0.0
        self._secret_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def _login(self) -> None:
        # The SPN token is fetched on the event loop (azure.identity.aio's get_token is a
        # coroutine); only the blocking hvac calls go to a worker thread.
        jwt = (await self._azure_cred.get_token(_VAULT_RESOURCE)).token

        def _do_login() -> tuple[hvac.Client, float]:
            client = hvac.Client(url=self._cfg.addr)
            resp = client.auth.azure.login(role=self._cfg.azure_auth_role, jwt=jwt,
                                           mount_point=self._cfg.mount_point)
            ttl = resp["auth"]["lease_duration"]
            return client, time.time() + ttl

        self._client, self._token_expiry = await asyncio.to_thread(_do_login)
        emit(CheckResult(event_type="engine_auth", source="vault", check_type="engine_auth",
                         status=CheckStatus.OK,
                         metadata={"token_ttl_s": round(self._token_expiry - time.time())}))

    async def _ensure_client(self) -> hvac.Client:
        async with self._lock:
            if self._client is None or time.time() > self._token_expiry - _RENEW_SAFETY_MARGIN:
                try:
                    await self._login()
                except Exception as exc:
                    emit(CheckResult(event_type="engine_auth", source="vault",
                                     check_type="engine_auth", status=CheckStatus.ERROR,
                                     error_type=ErrorType.AUTH, error_detail=str(exc)))
                    raise
            assert self._client is not None
            return self._client

    async def get_secret(self, path: str, mount: str = "secret") -> dict[str, Any]:
        """Read a KV-v2 secret, cached for _SECRET_CACHE_TTL seconds."""
        key = f"{mount}/{path}"
        cached = self._secret_cache.get(key)
        if cached and time.time() - cached[0] < _SECRET_CACHE_TTL:
            return cached[1]
        client = await self._ensure_client()
        resp = await asyncio.to_thread(
            client.secrets.kv.v2.read_secret_version, path=path, mount_point=mount
        )
        data: dict[str, Any] = resp["data"]["data"]
        self._secret_cache[key] = (time.time(), data)
        return data
