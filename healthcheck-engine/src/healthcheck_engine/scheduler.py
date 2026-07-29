from __future__ import annotations

import asyncio
import time

from .connectors.base import Connector
from .emitter import emit
from .schema import CheckResult, CheckStatus, EngineConfig, ErrorType, Heartbeat


class Scheduler:
    def __init__(self, cfg: EngineConfig, connectors: list[Connector]) -> None:
        self._cfg = cfg
        self._connectors = connectors
        self._ran_last_cycle: set[str] = set()

    async def _run_connector_loop(self, conn: Connector) -> None:
        while True:
            started = time.perf_counter()
            try:
                result = await asyncio.wait_for(conn.check(),
                                                timeout=conn.cfg.timeout_seconds + 5)
            except TimeoutError:
                result = CheckResult(source=conn.name, check_type=conn.cfg.type,
                                     status=CheckStatus.DOWN, error_type=ErrorType.TIMEOUT,
                                     error_detail="hard ceiling exceeded (connector hung)")
            except Exception as exc:
                result = CheckResult(source=conn.name, check_type=conn.cfg.type,
                                     status=CheckStatus.ERROR, error_type=ErrorType.INTERNAL,
                                     error_detail=repr(exc))
            try:
                emit(result)
            except Exception:
                # emit() itself must never be allowed to escape this loop: an unguarded
                # failure here (e.g. a non-JSON-serializable value slipping into
                # `result.metadata`) would propagate through asyncio.gather() in
                # Scheduler.run() and kill every other connector's loop along with it.
                # Fall back to a minimal, always-serializable CheckResult — deliberately
                # omitting the original metadata, since that's the likely culprit.
                try:
                    emit(CheckResult(source=conn.name, check_type=conn.cfg.type,
                                     status=CheckStatus.ERROR, error_type=ErrorType.INTERNAL,
                                     error_detail="failed to emit result"))
                except Exception:
                    pass
            self._ran_last_cycle.add(conn.name)
            elapsed = time.perf_counter() - started
            await asyncio.sleep(max(1.0, conn.cfg.interval_seconds - elapsed))

    async def _heartbeat_loop(self) -> None:
        while True:
            emit(Heartbeat(connectors_configured=len(self._connectors),
                           connectors_ran_last_cycle=len(self._ran_last_cycle)))
            self._ran_last_cycle.clear()
            await asyncio.sleep(self._cfg.heartbeat_interval_seconds)

    async def run(self) -> None:
        tasks = [asyncio.create_task(self._run_connector_loop(c)) for c in self._connectors]
        tasks.append(asyncio.create_task(self._heartbeat_loop()))
        await asyncio.gather(*tasks)
