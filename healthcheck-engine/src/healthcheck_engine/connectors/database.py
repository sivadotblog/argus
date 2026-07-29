"""params:
engine: str = "postgres"       — dispatches to an engine-specific _check_<engine>()
                                  method; adding a new engine later means adding one
                                  method + one entry in _ENGINE_CHECKS, not
                                  restructuring the class.
host: str                      (required for postgres)
port: int = 5432
database: str                  (required for postgres)
vault_path: str                (required) — same meaning as in auth_api.py: secret
                                  at this Vault path has "username"/"password" keys.
vault_mount: str = "secret"
degraded_latency_ms: float = 500   — DB checks should be fast: a SELECT 1 over an
                                      established-per-check connection taking >500ms
                                      already signals trouble (lower than the HTTP
                                      connectors' 2000ms default).

Every DB op happens inside the scheduler's wait_for(timeout_seconds) ceiling — hung
connections are the norm, not the exception, so asyncpg.connect() is itself given
timeout=self.cfg.timeout_seconds as a second line of defense.
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import ClassVar

import asyncpg

from ..schema import CheckResult, CheckStatus, ErrorType
from .base import Connector


class DatabaseConnector(Connector):
    async def check(self) -> CheckResult:
        p = self.cfg.params
        engine: str = p.get("engine", "postgres")

        assert self.creds is not None, f"{self.name}: database connector requires vault config"
        try:
            secret = await self.creds.get_secret(p["vault_path"], p.get("vault_mount", "secret"))
        except Exception as exc:
            return CheckResult(source=self.name, check_type=self.cfg.type,
                               status=CheckStatus.ERROR, error_type=ErrorType.AUTH,
                               error_detail=str(exc)[:500])

        check_fn = self._ENGINE_CHECKS.get(engine)
        if check_fn is None:
            return CheckResult(source=self.name, check_type=self.cfg.type,
                               status=CheckStatus.ERROR, error_type=ErrorType.INTERNAL,
                               error_detail=f"unknown engine: {engine!r}")
        return await check_fn(self, secret)

    async def _check_postgres(self, secret: dict[str, str]) -> CheckResult:
        p = self.cfg.params
        host: str = p["host"]
        port: int = p.get("port", 5432)
        database: str = p["database"]
        degraded_latency_ms: float = p.get("degraded_latency_ms", 500)
        metadata = {"engine": "postgres", "host": host, "database": database}

        start = time.perf_counter()
        conn: asyncpg.Connection | None = None
        try:
            conn = await asyncpg.connect(
                host=host, port=port, user=secret["username"], password=secret["password"],
                database=database, timeout=self.cfg.timeout_seconds,
            )
            await conn.fetchval("SELECT 1")
            latency = (time.perf_counter() - start) * 1000
            status = CheckStatus.DEGRADED if latency > degraded_latency_ms else CheckStatus.OK
            return CheckResult(source=self.name, check_type=self.cfg.type, status=status,
                               latency_ms=latency, metadata=metadata)
        except asyncpg.InvalidAuthorizationSpecificationError:
            # Covers InvalidPasswordError and other auth-related asyncpg exceptions —
            # they all derive from InvalidAuthorizationSpecificationError. Deliberately
            # not surfacing the exception text: Postgres auth-failure messages can echo
            # back the username (e.g. 'password authentication failed for user "x"'), and
            # credentials must never reach metadata/error_detail (they go to stdout/Splunk).
            return CheckResult(source=self.name, check_type=self.cfg.type,
                               status=CheckStatus.ERROR, error_type=ErrorType.AUTH,
                               error_detail="postgres authentication failed",
                               latency_ms=(time.perf_counter() - start) * 1000, metadata=metadata)
        except TimeoutError:
            # asyncpg raises the stdlib TimeoutError (asyncio.TimeoutError is an alias
            # of it on the Python versions this project targets) on connect timeout.
            return CheckResult(source=self.name, check_type=self.cfg.type,
                               status=CheckStatus.DOWN, error_type=ErrorType.TIMEOUT,
                               latency_ms=(time.perf_counter() - start) * 1000, metadata=metadata)
        except OSError as exc:
            # Connection refused, DNS failure, network unreachable, etc.
            return CheckResult(source=self.name, check_type=self.cfg.type,
                               status=CheckStatus.ERROR, error_type=ErrorType.CONNECTION,
                               error_detail=repr(exc)[:500],
                               latency_ms=(time.perf_counter() - start) * 1000, metadata=metadata)
        except Exception as exc:
            return CheckResult(source=self.name, check_type=self.cfg.type,
                               status=CheckStatus.ERROR, error_type=ErrorType.INTERNAL,
                               error_detail=repr(exc)[:500],
                               latency_ms=(time.perf_counter() - start) * 1000, metadata=metadata)
        finally:
            if conn is not None:
                # asyncpg's close() is not exception-safe: on a connection that's
                # already gone bad it internally _abort()s (so the socket is still
                # cleaned up either way) but then re-raises. A raise out of `finally`
                # would escape check()'s own try/except and violate the "must never
                # raise" contract from base.py, so swallow it here — the connector
                # already reports its own outcome via the CheckResult returned above.
                try:
                    await conn.close()
                except Exception:
                    pass

    _ENGINE_CHECKS: ClassVar[
        dict[str, Callable[[DatabaseConnector, dict[str, str]], Awaitable[CheckResult]]]
    ] = {
        "postgres": _check_postgres,
    }
