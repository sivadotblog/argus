from __future__ import annotations

import time

import httpx

from ..schema import CheckResult, CheckStatus, ErrorType
from .base import Connector


class PublicApiConnector(Connector):
    """params:
    url: str                      (required)
    method: str = "GET"
    expected_status: list[int] = [200]
    degraded_latency_ms: float = 2000   # slower than this but successful → DEGRADED
    """

    async def check(self) -> CheckResult:
        p = self.cfg.params
        url: str = p["url"]
        method: str = p.get("method", "GET")
        expected: list[int] = p.get("expected_status", [200])
        degraded_ms: float = p.get("degraded_latency_ms", 2000)

        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.cfg.timeout_seconds) as client:
                resp = await client.request(method, url)
            latency = (time.perf_counter() - start) * 1000
            if resp.status_code not in expected:
                return CheckResult(source=self.name, check_type=self.cfg.type,
                                   status=CheckStatus.DOWN, latency_ms=latency,
                                   error_type=ErrorType.HTTP_STATUS,
                                   error_detail=f"HTTP {resp.status_code}",
                                   metadata={"url": url, "status_code": resp.status_code})
            status = CheckStatus.DEGRADED if latency > degraded_ms else CheckStatus.OK
            return CheckResult(source=self.name, check_type=self.cfg.type, status=status,
                               latency_ms=latency,
                               metadata={"url": url, "status_code": resp.status_code})
        except httpx.TimeoutException:
            return CheckResult(source=self.name, check_type=self.cfg.type,
                               status=CheckStatus.DOWN,
                               latency_ms=(time.perf_counter() - start) * 1000,
                               error_type=ErrorType.TIMEOUT, metadata={"url": url})
        except httpx.TransportError as exc:
            # connection refused / DNS / TLS — often network policy, not target
            return CheckResult(source=self.name, check_type=self.cfg.type,
                               status=CheckStatus.ERROR, error_type=ErrorType.CONNECTION,
                               error_detail=str(exc), metadata={"url": url})
