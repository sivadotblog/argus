"""Tests for DatabaseConnector: asyncpg.connect is mocked — no real database.
Vault is mocked via a fake CredentialManager, same pattern as test_auth_api.py.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from healthcheck_engine.connectors.database import DatabaseConnector
from healthcheck_engine.schema import CheckStatus, ConnectorConfig, ErrorType

SECRET = {"username": "dbuser", "password": "s3cr3t-pw"}


class FakeCredentialManager:
    """Stands in for credentials.CredentialManager: async get_secret(path, mount)."""

    def __init__(self, secret: dict[str, str]) -> None:
        self._secret = secret
        self.calls: list[tuple[str, str]] = []

    async def get_secret(self, path: str, mount: str = "secret") -> dict[str, Any]:
        self.calls.append((path, mount))
        return self._secret


class FailingCredentialManager:
    """Simulates a Vault outage/auth failure during secret retrieval."""

    async def get_secret(self, path: str, mount: str = "secret") -> dict[str, Any]:
        raise RuntimeError("vault unreachable")


class FakeConnection:
    """Stands in for asyncpg.Connection: fetchval + close, both awaitable."""

    def __init__(self, fetchval_result: object = 1, fetchval_exc: Exception | None = None,
                 on_sleep: float = 0.0, close_exc: Exception | None = None) -> None:
        self._fetchval_result = fetchval_result
        self._fetchval_exc = fetchval_exc
        self._on_sleep = on_sleep
        self._close_exc = close_exc
        self.closed = False

    async def fetchval(self, query: str) -> object:
        if self._on_sleep:
            import asyncio
            await asyncio.sleep(self._on_sleep)
        if self._fetchval_exc is not None:
            raise self._fetchval_exc
        return self._fetchval_result

    async def close(self) -> None:
        self.closed = True
        if self._close_exc is not None:
            raise self._close_exc


def make_cfg(**params: object) -> ConnectorConfig:
    return ConnectorConfig(
        name="example-db",
        type="database",
        timeout_seconds=5.0,
        params={
            "host": "db.internal.example.com",
            "port": 5432,
            "database": "appdb",
            "vault_path": "healthchecks/example-db",
            **params,
        },
    )


async def test_ok_fast_query() -> None:
    fake_conn = FakeConnection()
    creds = FakeCredentialManager(SECRET)
    connector = DatabaseConnector(make_cfg(), creds)  # type: ignore[arg-type]

    with patch("asyncpg.connect", new=AsyncMock(return_value=fake_conn)) as mock_connect:
        result = await connector.check()

    assert result.status == CheckStatus.OK
    assert result.error_type == ErrorType.NONE
    assert result.metadata == {
        "engine": "postgres", "host": "db.internal.example.com", "database": "appdb",
    }
    assert fake_conn.closed is True
    mock_connect.assert_awaited_once()
    _, kwargs = mock_connect.call_args
    assert kwargs["user"] == "dbuser"
    assert kwargs["password"] == "s3cr3t-pw"


async def test_degraded_when_latency_exceeds_threshold() -> None:
    fake_conn = FakeConnection(on_sleep=0.05)
    creds = FakeCredentialManager(SECRET)
    connector = DatabaseConnector(make_cfg(degraded_latency_ms=1), creds)  # type: ignore[arg-type]

    with patch("asyncpg.connect", new=AsyncMock(return_value=fake_conn)):
        result = await connector.check()

    assert result.status == CheckStatus.DEGRADED
    assert result.error_type == ErrorType.NONE
    assert fake_conn.closed is True


async def test_default_degraded_threshold_is_2000ms_connect_inclusive_not_500ms() -> None:
    # 600ms of (mocked) connect-inclusive latency must stay OK under the current 2000ms
    # default — under the old 500ms default this would have been wrongly DEGRADED, which
    # is exactly the false-alarm behavior this default change fixes for realistic
    # cross-AZ/cross-region Postgres connect times.
    fake_conn = FakeConnection()
    creds = FakeCredentialManager(SECRET)
    connector = DatabaseConnector(make_cfg(), creds)  # type: ignore[arg-type]

    with (
        patch("asyncpg.connect", new=AsyncMock(return_value=fake_conn)),
        patch(
            "healthcheck_engine.connectors.database.time.perf_counter",
            side_effect=[0.0, 0.6],
        ),
    ):
        result = await connector.check()

    assert result.status == CheckStatus.OK
    assert result.latency_ms == 600.0


async def test_connect_timeout_is_down_with_timeout_error_type() -> None:
    creds = FakeCredentialManager(SECRET)
    connector = DatabaseConnector(make_cfg(), creds)  # type: ignore[arg-type]

    with patch("asyncpg.connect", new=AsyncMock(side_effect=TimeoutError("timed out"))):
        result = await connector.check()

    assert result.status == CheckStatus.DOWN
    assert result.error_type == ErrorType.TIMEOUT


async def test_invalid_password_is_error_with_auth_error_type() -> None:
    creds = FakeCredentialManager(SECRET)
    connector = DatabaseConnector(make_cfg(), creds)  # type: ignore[arg-type]

    exc = asyncpg.InvalidPasswordError("password authentication failed for user \"dbuser\"")
    with patch("asyncpg.connect", new=AsyncMock(side_effect=exc)):
        result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.AUTH


async def test_connection_refused_is_error_with_connection_error_type() -> None:
    creds = FakeCredentialManager(SECRET)
    connector = DatabaseConnector(make_cfg(), creds)  # type: ignore[arg-type]

    with patch("asyncpg.connect", new=AsyncMock(side_effect=ConnectionRefusedError("refused"))):
        result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.CONNECTION


async def test_fetchval_failure_still_closes_connection() -> None:
    fake_conn = FakeConnection(fetchval_exc=RuntimeError("boom"))
    creds = FakeCredentialManager(SECRET)
    connector = DatabaseConnector(make_cfg(), creds)  # type: ignore[arg-type]

    with patch("asyncpg.connect", new=AsyncMock(return_value=fake_conn)):
        result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.INTERNAL
    assert fake_conn.closed is True


async def test_close_raising_after_fetchval_failure_does_not_escape_check() -> None:
    """asyncpg's Connection.close() is not exception-safe: on an already-bad connection
    it internally _abort()s but then re-raises. check() must swallow that so a failed
    close can never violate the "must never raise" contract from base.py — it must
    return a CheckResult (from the original fetchval failure), not raise out of check().
    """
    fake_conn = FakeConnection(fetchval_exc=RuntimeError("boom"),
                               close_exc=RuntimeError("close also failed"))
    creds = FakeCredentialManager(SECRET)
    connector = DatabaseConnector(make_cfg(), creds)  # type: ignore[arg-type]

    with patch("asyncpg.connect", new=AsyncMock(return_value=fake_conn)):
        result = await connector.check()  # must not raise

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.INTERNAL
    assert fake_conn.closed is True


async def test_unknown_engine_is_error_with_internal_error_type() -> None:
    creds = FakeCredentialManager(SECRET)
    connector = DatabaseConnector(make_cfg(engine="mysql"), creds)  # type: ignore[arg-type]

    result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.INTERNAL
    assert "mysql" in (result.error_detail or "")


async def test_missing_creds_is_error_with_auth_error_type_and_does_not_raise() -> None:
    # self.creds is None (e.g. no vault: block in EngineConfig) — check() must return a
    # CheckResult, not raise AssertionError, per the "must never raise" contract in base.py.
    connector = DatabaseConnector(make_cfg(), None)  # type: ignore[arg-type]

    with patch("asyncpg.connect", new=AsyncMock()) as mock_connect:
        result = await connector.check()  # must not raise

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.AUTH
    assert "vault config" in (result.error_detail or "")
    mock_connect.assert_not_awaited()


async def test_vault_secret_failure_is_error_with_auth_error_type_and_no_connect_attempt() -> None:
    connector = DatabaseConnector(make_cfg(), FailingCredentialManager())  # type: ignore[arg-type]

    with patch("asyncpg.connect", new=AsyncMock()) as mock_connect:
        result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.AUTH
    assert "vault unreachable" in (result.error_detail or "")
    mock_connect.assert_not_awaited()


@pytest.mark.parametrize(
    "scenario", ["ok", "invalid_password", "connect_timeout", "fetchval_error"]
)
async def test_credentials_never_leak_into_metadata_or_error_detail(scenario: str) -> None:
    creds = FakeCredentialManager(SECRET)
    connector = DatabaseConnector(make_cfg(), creds)  # type: ignore[arg-type]

    if scenario == "ok":
        fake_conn = FakeConnection()
        patched = patch("asyncpg.connect", new=AsyncMock(return_value=fake_conn))
    elif scenario == "invalid_password":
        # Realistic asyncpg/Postgres wire text: the username IS echoed back by the
        # server on auth failure (password itself never is) — this is the exact case
        # the fixed error_detail in the AUTH branch exists to defuse.
        exc = asyncpg.InvalidPasswordError(
            f'password authentication failed for user "{SECRET["username"]}"'
        )
        patched = patch("asyncpg.connect", new=AsyncMock(side_effect=exc))
    elif scenario == "connect_timeout":
        patched = patch("asyncpg.connect", new=AsyncMock(side_effect=TimeoutError()))
    else:
        fake_conn = FakeConnection(fetchval_exc=RuntimeError("query failed: syntax error"))
        patched = patch("asyncpg.connect", new=AsyncMock(return_value=fake_conn))

    with patched:
        result = await connector.check()

    dumped = result.model_dump_json()
    assert SECRET["username"] not in dumped
    assert SECRET["password"] not in dumped
