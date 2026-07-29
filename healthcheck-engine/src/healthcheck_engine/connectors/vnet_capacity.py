from __future__ import annotations

import contextlib
import ipaddress
import time
from typing import Any

from azure.core.exceptions import AzureError, ClientAuthenticationError
from azure.identity.aio import ClientSecretCredential
from azure.mgmt.network.aio import NetworkManagementClient

from ..credentials import CredentialManager, resolve_spn_credential
from ..schema import CheckResult, CheckStatus, ConnectorConfig, ErrorType
from .base import Connector

# Azure reserves 5 IPs per subnet: network address, gateway, two DNS-reserved, broadcast.
_RESERVED_IPS_PER_SUBNET = 5


class VnetCapacityConnector(Connector):
    """params:
    vnets: list[dict]             (required) — one entry per subnet to check:
                                   {"subscription_id": str, "resource_group": str,
                                    "vnet_name": str, "subnet_name": str}
    degraded_pct: float = 80
    down_pct: float = 95
    vault_path: str                (optional) — Vault path of a DISTINCT SPN to use for
                                   this check instead of the shared engine SPN; the secret
                                   must have "tenant_id"/"client_id"/"client_secret" keys.
    vault_mount: str = "secret"    (only meaningful together with vault_path)

    Authenticates with an explicit service principal (azure.identity.aio.
    ClientSecretCredential) — never DefaultAzureCredential, whose cascading resolution is
    unpredictable in a container. By default that is the shared engine SPN from
    AZURE_TENANT_ID/AZURE_CLIENT_ID/AZURE_CLIENT_SECRET; setting `vault_path` sources a
    distinct SPN from Vault instead (see credentials.resolve_spn_credential).

    Uses azure.mgmt.network.aio.NetworkManagementClient (the async client — matches the
    asyncio scheduler model) to read each configured subnet's address_prefix and
    ip_configurations. The installed SDK's Subnet model does not expose a ready-made
    available-IP-count property, so usage is computed from address_prefix (total usable IPs,
    minus Azure's 5 reserved) and len(ip_configurations) (used IPs), per the plan.

    Credential lifecycle: per the plan's settled architecture ("tokens cached and renewed,
    never fetched per-check"), the ClientSecretCredential is built lazily on the first
    check() and then cached on the instance for the lifetime of this long-running process —
    never re-created or closed inside check() (ClientSecretCredential refreshes its own
    tokens internally). No lock is needed: the scheduler runs a given connector's check()
    calls serially, never concurrently with itself. Only the lightweight per-subscription
    NetworkManagementClient wrappers are created fresh each check (via AsyncExitStack),
    since they just wrap the shared credential.

    One connector instance can check multiple subnets, possibly across subscriptions — a
    NetworkManagementClient is created (and cached) per distinct subscription_id. The overall
    CheckResult.status is the worst status across all configured subnets; metadata["subnets"]
    carries the per-subnet breakdown.

    Fail-soft: any Azure SDK/auth failure maps to status=ERROR (never raises) — credential
    sourcing failures (missing env vars, missing vault config, Vault read failure, Vault
    secret missing keys) and ClientAuthenticationError -> error_type=AUTH, other
    azure.core.exceptions.AzureError -> error_type=CONNECTION, anything else unanticipated
    -> error_type=INTERNAL.
    """

    def __init__(self, cfg: ConnectorConfig, creds: CredentialManager | None) -> None:
        super().__init__(cfg, creds)
        # Populated on the first check() and reused thereafter — see class docstring.
        self._credential: ClientSecretCredential | None = None

    async def _get_credential(self) -> ClientSecretCredential:
        if self._credential is None:
            self._credential = await resolve_spn_credential(self.cfg.params, self.creds)
        return self._credential

    async def check(self) -> CheckResult:
        p = self.cfg.params
        vnets: list[dict[str, str]] = p["vnets"]
        degraded_pct: float = p.get("degraded_pct", 80)
        down_pct: float = p.get("down_pct", 95)

        start = time.perf_counter()
        try:
            credential = await self._get_credential()
        except Exception as exc:
            # Everything that can go wrong while sourcing the SPN is an auth-domain
            # problem: missing env vars, vault_path set without vault config, a failed
            # Vault read, or a Vault secret missing tenant_id/client_id/client_secret.
            return CheckResult(
                source=self.name,
                check_type=self.cfg.type,
                status=CheckStatus.ERROR,
                error_type=ErrorType.AUTH,
                error_detail=str(exc)[:500],
                latency_ms=(time.perf_counter() - start) * 1000,
            )
        try:
            subnet_results: list[dict[str, Any]] = []
            async with contextlib.AsyncExitStack() as stack:
                clients: dict[str, NetworkManagementClient] = {}
                for vnet in vnets:
                    sub_id = vnet["subscription_id"]
                    client = clients.get(sub_id)
                    if client is None:
                        client = await stack.enter_async_context(
                            NetworkManagementClient(credential, sub_id)
                        )
                        clients[sub_id] = client

                    subnet = await client.subnets.get(
                        vnet["resource_group"], vnet["vnet_name"], vnet["subnet_name"]
                    )
                    prefix = subnet.address_prefix
                    if not prefix:
                        raise ValueError(
                            f"subnet {vnet['subnet_name']!r} returned no address_prefix"
                        )
                    prefix_len = ipaddress.ip_network(prefix, strict=False).prefixlen
                    total = max(2 ** (32 - prefix_len) - _RESERVED_IPS_PER_SUBNET, 0)
                    used = len(subnet.ip_configurations or [])
                    pct_used = (used / total * 100) if total > 0 else 100.0
                    subnet_results.append(
                        {
                            "subnet": vnet["subnet_name"],
                            "total": total,
                            "used": used,
                            "available": max(total - used, 0),
                            "pct_used": round(pct_used, 2),
                        }
                    )

            latency = (time.perf_counter() - start) * 1000
            worst_pct = max((s["pct_used"] for s in subnet_results), default=0.0)
            if worst_pct >= down_pct:
                status = CheckStatus.DOWN
            elif worst_pct >= degraded_pct:
                status = CheckStatus.DEGRADED
            else:
                status = CheckStatus.OK
            return CheckResult(
                source=self.name,
                check_type=self.cfg.type,
                status=status,
                latency_ms=latency,
                metadata={"subnets": subnet_results},
            )
        except ClientAuthenticationError as exc:
            return CheckResult(
                source=self.name,
                check_type=self.cfg.type,
                status=CheckStatus.ERROR,
                error_type=ErrorType.AUTH,
                error_detail=str(exc)[:500],
                latency_ms=(time.perf_counter() - start) * 1000,
            )
        except AzureError as exc:
            return CheckResult(
                source=self.name,
                check_type=self.cfg.type,
                status=CheckStatus.ERROR,
                error_type=ErrorType.CONNECTION,
                error_detail=str(exc)[:500],
                latency_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            # Broad catch is deliberate, matching the pattern in azure_status.py: this connector
            # must never raise (base.py contract), and a code-generated SDK can surface exception
            # types beyond azure.core's hierarchy (e.g. our own ValueError above, or a malformed
            # mock/response shape) — those are engine-side failures, not target-down evidence.
            return CheckResult(
                source=self.name,
                check_type=self.cfg.type,
                status=CheckStatus.ERROR,
                error_type=ErrorType.INTERNAL,
                error_detail=str(exc)[:500],
                latency_ms=(time.perf_counter() - start) * 1000,
            )
