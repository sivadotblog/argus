from __future__ import annotations

import abc

from ..credentials import CredentialManager
from ..schema import CheckResult, ConnectorConfig


class Connector(abc.ABC):
    """One instance per configured check target."""

    def __init__(self, cfg: ConnectorConfig, creds: CredentialManager | None) -> None:
        self.cfg = cfg
        self.creds = creds

    @property
    def name(self) -> str:
        return self.cfg.name

    @abc.abstractmethod
    async def check(self) -> CheckResult:
        """Run one check. Must not raise — map failures into CheckResult.
        The scheduler additionally wraps this in asyncio.wait_for(timeout_seconds).
        """
