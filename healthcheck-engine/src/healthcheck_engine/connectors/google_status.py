from __future__ import annotations

import time
from typing import Any

import httpx

from ..schema import CheckResult, CheckStatus, ErrorType
from .base import Connector

GOOGLE_INCIDENTS_URL = "https://status.cloud.google.com/incidents.json"


class GoogleStatusConnector(Connector):
    """params:
    services: list[str]            (required) — Google Cloud service names to filter on,
                                    matched against each incident's `service_name` field
                                    (as it appears in incidents.json).
    active_only: bool = True       — if true, only incidents with no `end` timestamp
                                    (still ongoing) count as affecting status.

    Fetches https://status.cloud.google.com/incidents.json and looks for incidents
    affecting the configured services.

    Severity heuristic: Google's real incidents.json exposes a free-text `severity`
    field (observed values include "high"/"medium"/"low") but no explicit boolean
    "is this an outage" flag. We treat any matching incident whose severity mentions
    "high" (case-insensitive) as DOWN; every other matching incident (medium/low
    severity, or severity missing/unrecognized) is DEGRADED rather than OK, since it
    is still an active incident touching a service we care about.
    """

    async def check(self) -> CheckResult:
        p = self.cfg.params
        services: list[str] = p["services"]
        active_only: bool = p.get("active_only", True)

        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.cfg.timeout_seconds) as client:
                resp = await client.get(GOOGLE_INCIDENTS_URL)
            latency = (time.perf_counter() - start) * 1000
        except httpx.TimeoutException:
            return CheckResult(source=self.name, check_type=self.cfg.type,
                               status=CheckStatus.DOWN, error_type=ErrorType.TIMEOUT,
                               latency_ms=(time.perf_counter() - start) * 1000,
                               metadata={"url": GOOGLE_INCIDENTS_URL})
        except httpx.TransportError as exc:
            return CheckResult(source=self.name, check_type=self.cfg.type,
                               status=CheckStatus.ERROR, error_type=ErrorType.CONNECTION,
                               error_detail=str(exc), metadata={"url": GOOGLE_INCIDENTS_URL})

        if resp.status_code != 200:
            return CheckResult(source=self.name, check_type=self.cfg.type,
                               status=CheckStatus.DOWN, latency_ms=latency,
                               error_type=ErrorType.HTTP_STATUS,
                               error_detail=f"HTTP {resp.status_code}",
                               metadata={"url": GOOGLE_INCIDENTS_URL,
                                         "status_code": resp.status_code})

        try:
            incidents: Any = resp.json()
            matched: list[dict[str, Any]] = []
            for incident in incidents:
                service_name = incident["service_name"]
                if service_name not in services:
                    continue
                if active_only and incident.get("end") is not None:
                    continue
                matched.append(incident)

            if not matched:
                return CheckResult(source=self.name, check_type=self.cfg.type,
                                   status=CheckStatus.OK, latency_ms=latency,
                                   metadata={"url": GOOGLE_INCIDENTS_URL})

            is_high_severity = any(
                "high" in str(incident.get("severity", "")).lower() for incident in matched
            )
            status = CheckStatus.DOWN if is_high_severity else CheckStatus.DEGRADED
            matched_services = sorted({incident["service_name"] for incident in matched})
            return CheckResult(source=self.name, check_type=self.cfg.type, status=status,
                               latency_ms=latency,
                               metadata={"url": GOOGLE_INCIDENTS_URL,
                                         "incident_count": len(matched),
                                         "matched_services": matched_services})
        except (KeyError, TypeError, ValueError) as exc:
            # Broad catch is deliberate: incidents.json is an unversioned public feed
            # and its shape can change without notice — fail soft, never raise.
            return CheckResult(source=self.name, check_type=self.cfg.type,
                               status=CheckStatus.UNKNOWN, latency_ms=latency,
                               error_type=ErrorType.PARSE, error_detail=str(exc)[:500],
                               metadata={"url": GOOGLE_INCIDENTS_URL})
