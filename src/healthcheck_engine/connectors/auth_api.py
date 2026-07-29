from __future__ import annotations

import time

import httpx

from ..schema import CheckResult, CheckStatus, ErrorType
from .base import Connector


class AuthApiConnector(Connector):
    """params:
    url: str                       (required)
    method: str = "GET"
    expected_status: list[int] = [200]
    degraded_latency_ms: float = 2000
    vault_path: str                (required) e.g. "healthchecks/github-api"
    vault_mount: str = "secret"
    auth_style: "bearer" | "basic" | "header" = "bearer"
    # bearer  → secret key "token"        → Authorization: Bearer <token>
    # basic   → secret keys "username","password"
    # header  → secret keys "header_name","header_value"
    """

    async def _auth_headers_and_basic(self) -> tuple[dict[str, str], tuple[str, str] | None]:
        p = self.cfg.params
        assert self.creds is not None, f"{self.name}: auth_api requires vault config"
        secret = await self.creds.get_secret(p["vault_path"], p.get("vault_mount", "secret"))
        style = p.get("auth_style", "bearer")
        if style == "bearer":
            return {"Authorization": f"Bearer {secret['token']}"}, None
        if style == "basic":
            return {}, (secret["username"], secret["password"])
        if style == "header":
            return {secret["header_name"]: secret["header_value"]}, None
        raise ValueError(f"unknown auth_style: {style}")

    async def check(self) -> CheckResult:
        p = self.cfg.params
        url: str = p["url"]
        start = time.perf_counter()
        try:
            headers, basic = await self._auth_headers_and_basic()
        except Exception as exc:
            return CheckResult(source=self.name, check_type=self.cfg.type,
                               status=CheckStatus.ERROR, error_type=ErrorType.AUTH,
                               error_detail=str(exc), metadata={"url": url})
        try:
            async with httpx.AsyncClient(timeout=self.cfg.timeout_seconds) as client:
                resp = await client.request(p.get("method", "GET"), url,
                                            headers=headers, auth=basic)
            latency = (time.perf_counter() - start) * 1000
            expected = p.get("expected_status", [200])
            if resp.status_code in (401, 403):
                error_msg = f"HTTP {resp.status_code} — credential likely stale"
                return CheckResult(source=self.name, check_type=self.cfg.type,
                                   status=CheckStatus.ERROR, latency_ms=latency,
                                   error_type=ErrorType.AUTH, error_detail=error_msg,
                                   metadata={"url": url, "status_code": resp.status_code})
            if resp.status_code not in expected:
                return CheckResult(source=self.name, check_type=self.cfg.type,
                                   status=CheckStatus.DOWN, latency_ms=latency,
                                   error_type=ErrorType.HTTP_STATUS,
                                   error_detail=f"HTTP {resp.status_code}",
                                   metadata={"url": url, "status_code": resp.status_code})
            status = (CheckStatus.DEGRADED
                      if latency > p.get("degraded_latency_ms", 2000) else CheckStatus.OK)
            return CheckResult(source=self.name, check_type=self.cfg.type, status=status,
                               latency_ms=latency,
                               metadata={"url": url, "status_code": resp.status_code})
        except httpx.TimeoutException:
            return CheckResult(source=self.name, check_type=self.cfg.type,
                               status=CheckStatus.DOWN, error_type=ErrorType.TIMEOUT,
                               latency_ms=(time.perf_counter() - start) * 1000,
                               metadata={"url": url})
        except httpx.TransportError as exc:
            return CheckResult(source=self.name, check_type=self.cfg.type,
                               status=CheckStatus.ERROR, error_type=ErrorType.CONNECTION,
                               error_detail=str(exc), metadata={"url": url})
