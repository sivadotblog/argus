from __future__ import annotations

import contextlib
import re
import time
from typing import Any

from azure.core.exceptions import AzureError, ClientAuthenticationError
from azure.identity.aio import DefaultAzureCredential
from azure.mgmt.quota.aio import QuotaMgmtClient
from azure.mgmt.quota.models import LimitObject

from ..schema import CheckResult, CheckStatus, ErrorType
from .base import Connector

_SUBSCRIPTION_ID_RE = re.compile(r"/subscriptions/([^/]+)", re.IGNORECASE)


def _extract_subscription_id(scope: str) -> str:
    match = _SUBSCRIPTION_ID_RE.search(scope)
    if not match:
        raise ValueError(f"could not extract subscription_id from scope: {scope!r}")
    return match.group(1)


class QuotaConnector(Connector):
    """params:
    scopes: list[dict]             (required) — one entry per quota resource to check:
                                    {"scope": str, "resource_name": str}
                                    scope is the ARM resource-provider scope, e.g.
                                    "/subscriptions/{sub}/providers/Microsoft.Compute/locations/{loc}"
                                    resource_name identifies the quota resource, e.g.
                                    "standardDSv3Family".
    degraded_pct: float = 80
    down_pct: float = 95

    Uses DefaultAzureCredential directly (no Vault) with azure.mgmt.quota.aio.QuotaMgmtClient
    (the async client) to read current usage (usages.get) and limit (quota.get) for each
    configured scope/resource_name pair, computing pct_used = usage / limit * 100.

    QuotaMgmtClient's constructor requires a subscription_id even though the usages/quota REST
    calls are fully addressed by the `scope` string and don't otherwise use it — we extract it
    from each entry's scope so config doesn't have to duplicate it, and cache one client per
    distinct subscription_id (scopes can span subscriptions).

    Overall CheckResult.status is the worst status across all configured scopes;
    metadata["scopes"] carries the per-scope breakdown.

    Fail-soft: any Azure SDK/auth failure maps to status=ERROR (never raises) —
    ClientAuthenticationError -> error_type=AUTH, other azure.core.exceptions.AzureError ->
    error_type=CONNECTION, anything else unanticipated -> error_type=INTERNAL.
    """

    async def check(self) -> CheckResult:
        p = self.cfg.params
        scopes: list[dict[str, str]] = p["scopes"]
        degraded_pct: float = p.get("degraded_pct", 80)
        down_pct: float = p.get("down_pct", 95)

        start = time.perf_counter()
        try:
            scope_results: list[dict[str, Any]] = []
            async with contextlib.AsyncExitStack() as stack:
                credential = await stack.enter_async_context(DefaultAzureCredential())
                clients: dict[str, QuotaMgmtClient] = {}
                for entry in scopes:
                    scope = entry["scope"]
                    resource_name = entry["resource_name"]
                    sub_id = _extract_subscription_id(scope)
                    client = clients.get(sub_id)
                    if client is None:
                        client = await stack.enter_async_context(
                            QuotaMgmtClient(credential, sub_id)
                        )
                        clients[sub_id] = client

                    usage_result = await client.usages.get(resource_name, scope)
                    limit_result = await client.quota.get(resource_name, scope)

                    usage_props = usage_result.properties
                    if usage_props is None or usage_props.usages is None:
                        raise ValueError(f"no usage data for {resource_name!r} at {scope!r}")
                    usage_value = usage_props.usages.value

                    limit_props = limit_result.properties
                    if limit_props is None or limit_props.limit is None:
                        raise ValueError(f"no limit data for {resource_name!r} at {scope!r}")
                    limit_obj = limit_props.limit
                    # `limit` is typed as the discriminated base LimitJsonObject; LimitObject
                    # (discriminator "LimitValue") is the only known subclass and the one that
                    # actually carries `.value` — narrow with isinstance rather than a blind
                    # attribute access so mypy --strict is satisfied without a type: ignore.
                    if not isinstance(limit_obj, LimitObject):
                        raise ValueError(
                            f"unexpected limit object type for {resource_name!r}: "
                            f"{type(limit_obj).__name__}"
                        )
                    limit_value = limit_obj.value

                    pct_used = (usage_value / limit_value * 100) if limit_value > 0 else 100.0
                    scope_results.append(
                        {
                            "scope": scope,
                            "resource_name": resource_name,
                            "usage": usage_value,
                            "limit": limit_value,
                            "pct_used": round(pct_used, 2),
                        }
                    )

            latency = (time.perf_counter() - start) * 1000
            worst_pct = max((s["pct_used"] for s in scope_results), default=0.0)
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
                metadata={"scopes": scope_results},
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
            # must never raise (base.py contract). Covers our own ValueError above plus anything
            # a code-generated SDK / mocked response can throw beyond azure.core's hierarchy —
            # those are engine-side failures, not target-down evidence.
            return CheckResult(
                source=self.name,
                check_type=self.cfg.type,
                status=CheckStatus.ERROR,
                error_type=ErrorType.INTERNAL,
                error_detail=str(exc)[:500],
                latency_ms=(time.perf_counter() - start) * 1000,
            )
