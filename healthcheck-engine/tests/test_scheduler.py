"""Tests for Scheduler's emit() safety net: emit() failing must never escape
_run_connector_loop (it would otherwise propagate through asyncio.gather() in
Scheduler.run() and kill every other connector's loop with it).
"""
from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import patch

from healthcheck_engine.connectors.base import Connector
from healthcheck_engine.scheduler import Scheduler
from healthcheck_engine.schema import (
    CheckResult,
    CheckStatus,
    ConnectorConfig,
    EngineConfig,
    ErrorType,
)


class FakeConnector(Connector):
    """A connector whose check() always returns a fixed, pre-built CheckResult."""

    def __init__(self, cfg: ConnectorConfig, result: CheckResult) -> None:
        super().__init__(cfg, None)
        self._result = result

    async def check(self) -> CheckResult:
        return self._result


def make_cfg(**kwargs: object) -> ConnectorConfig:
    return ConnectorConfig(
        name="fake", type="fake", interval_seconds=1, timeout_seconds=1.0, **kwargs  # type: ignore[arg-type]
    )


async def _run_one_cycle_then_cancel(scheduler: Scheduler, conn: Connector) -> None:
    """Runs _run_connector_loop just long enough for one full iteration (both emit
    attempts happen synchronously before the loop's inter-cycle sleep), then cancels.
    """
    task = asyncio.create_task(scheduler._run_connector_loop(conn))
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task  # if the loop let an exception other than CancelledError escape,
        # this re-raises it and fails the test.


async def test_emit_failure_falls_back_to_minimal_error_result_and_loop_survives() -> None:
    cfg = make_cfg()
    result = CheckResult(source="fake", check_type="fake", status=CheckStatus.OK,
                         metadata={"maybe": "unserializable-in-real-life"})
    conn = FakeConnector(cfg, result)
    scheduler = Scheduler(EngineConfig(checks=[cfg]), [conn])

    emitted: list[CheckResult] = []

    def fake_emit(event: object) -> None:
        assert isinstance(event, CheckResult)
        emitted.append(event)
        if len(emitted) == 1:
            raise RuntimeError("simulated emit() failure (e.g. json.dumps blew up)")

    with patch("healthcheck_engine.scheduler.emit", side_effect=fake_emit):
        await _run_one_cycle_then_cancel(scheduler, conn)

    # First attempt was the real result (and failed); second was the fallback.
    assert len(emitted) == 2
    assert emitted[0] is result
    fallback = emitted[1]
    assert fallback.status == CheckStatus.ERROR
    assert fallback.error_type == ErrorType.INTERNAL
    assert fallback.error_detail == "failed to emit result"
    assert fallback.metadata == {}  # original (possibly-unserializable) metadata dropped
    assert fallback.source == conn.name
    assert fallback.check_type == conn.cfg.type

    # Bookkeeping right after the guarded emit still ran, i.e. the loop didn't die mid-iteration.
    assert "fake" in scheduler._ran_last_cycle


async def test_emit_and_fallback_emit_both_failing_still_does_not_escape_the_loop() -> None:
    # Even in the (extreme) case where the fallback emit ALSO fails, the loop must
    # survive: the inner try/except around the fallback swallows that too.
    cfg = make_cfg()
    result = CheckResult(source="fake", check_type="fake", status=CheckStatus.OK)
    conn = FakeConnector(cfg, result)
    scheduler = Scheduler(EngineConfig(checks=[cfg]), [conn])

    with patch("healthcheck_engine.scheduler.emit",
               side_effect=RuntimeError("emit is completely broken")):
        await _run_one_cycle_then_cancel(scheduler, conn)  # must not raise RuntimeError

    assert "fake" in scheduler._ran_last_cycle


async def test_successful_emit_is_not_affected_by_the_guard() -> None:
    cfg = make_cfg()
    result = CheckResult(source="fake", check_type="fake", status=CheckStatus.OK)
    conn = FakeConnector(cfg, result)
    scheduler = Scheduler(EngineConfig(checks=[cfg]), [conn])

    emitted: list[CheckResult] = []
    with patch("healthcheck_engine.scheduler.emit", side_effect=emitted.append):
        await _run_one_cycle_then_cancel(scheduler, conn)

    assert emitted == [result]
