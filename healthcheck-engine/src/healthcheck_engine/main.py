from __future__ import annotations

import asyncio
import os

import yaml

from .connectors import REGISTRY
from .credentials import CredentialManager
from .scheduler import Scheduler
from .schema import EngineConfig


def load_config(path: str) -> EngineConfig:
    with open(path) as f:
        return EngineConfig.model_validate(yaml.safe_load(f))


def main() -> None:
    cfg = load_config(os.environ.get("CHECKS_CONFIG", "/etc/healthcheck/checks.yaml"))
    creds = CredentialManager(cfg.vault) if cfg.vault else None
    connectors = []
    for check in cfg.checks:
        cls = REGISTRY.get(check.type)
        if cls is None:
            raise SystemExit(f"unknown connector type: {check.type!r} ({check.name})")
        connectors.append(cls(check, creds))
    asyncio.run(Scheduler(cfg, connectors).run())


if __name__ == "__main__":
    main()
