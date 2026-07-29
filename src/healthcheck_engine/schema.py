"""Pydantic models: the stdout JSON contract (schema_version 1) and config models.

RULE: never make a breaking change to CheckResult. Additive fields only.
If a breaking change is unavoidable, bump schema_version and coordinate Splunk side.
"""
from __future__ import annotations

import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class CheckStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"   # check ran but result unparseable (e.g. status page format changed)
    ERROR = "error"       # the ENGINE failed (auth, network policy, bug) — not the target


class ErrorType(StrEnum):
    NONE = "none"
    TIMEOUT = "timeout"
    CONNECTION = "connection"        # likely NSG/firewall/DNS — not necessarily target down
    AUTH = "auth"                    # vault/azure/service credential failure
    HTTP_STATUS = "http_status"
    PARSE = "parse"
    INTERNAL = "internal"


class CheckResult(BaseModel):
    schema_version: int = 1
    event_type: str = "check_result"           # "check_result" | "heartbeat" | "engine_auth"
    source: str                                # logical name from config, e.g. "vault-prod"
    check_type: str                            # connector type, e.g. "auth_api"
    status: CheckStatus
    latency_ms: float | None = None
    error_type: ErrorType = ErrorType.NONE
    error_detail: str | None = None            # truncated to 500 chars by emitter
    timestamp: float = Field(default_factory=time.time)
    metadata: dict[str, Any] = Field(default_factory=dict)  # connector-specific extras


class Heartbeat(BaseModel):
    schema_version: int = 1
    event_type: str = "heartbeat"
    engine: str = "healthcheck-engine"
    connectors_configured: int
    connectors_ran_last_cycle: int
    timestamp: float = Field(default_factory=time.time)


# ---------- config models (checks.yaml) ----------

class ConnectorConfig(BaseModel):
    name: str                       # unique logical name → becomes CheckResult.source
    type: str                       # key in connector registry, e.g. "public_api"
    interval_seconds: int = 120
    timeout_seconds: float = 10.0   # hard ceiling enforced by scheduler via asyncio.wait_for
    params: dict[str, Any] = Field(default_factory=dict)


class VaultConfig(BaseModel):
    addr: str
    azure_auth_role: str
    mount_point: str = "azure"


class EngineConfig(BaseModel):
    heartbeat_interval_seconds: int = 60
    vault: VaultConfig | None = None
    checks: list[ConnectorConfig]
