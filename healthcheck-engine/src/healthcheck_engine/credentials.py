"""Azure Workload Identity → Vault (azure auth method) → service secrets.

Design:
- Login to Vault once, cache the client, renew before TTL expiry.
- Per-secret cache with short TTL so every check doesn't hit Vault.
- Emits an `engine_auth` CheckResult so Splunk can alert on OUR auth breaking —
  the classic silent failure mode where everything 403s after N hours.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import hvac
from azure.identity import DefaultAzureCredential

from .emitter import emit
from .schema import CheckResult, CheckStatus, ErrorType, VaultConfig

_VAULT_RESOURCE = "https://management.azure.com/"
_SECRET_CACHE_TTL = 300  # seconds
_RENEW_SAFETY_MARGIN = 120  # renew this many seconds before token TTL expires


class CredentialManager:
    def __init__(self, cfg: VaultConfig) -> None:
        self._cfg = cfg
        self._azure_cred = DefaultAzureCredential()
        self._client: hvac.Client | None = None
        self._token_expiry: float = 0.0
        self._secret_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def _login(self) -> None:
        def _do_login() -> tuple[hvac.Client, float]:
            jwt = self._azure_cred.get_token(_VAULT_RESOURCE).token
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
