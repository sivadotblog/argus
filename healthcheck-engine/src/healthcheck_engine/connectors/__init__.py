from __future__ import annotations

from .auth_api import AuthApiConnector
from .base import Connector
from .public_api import PublicApiConnector

REGISTRY: dict[str, type[Connector]] = {
    "public_api": PublicApiConnector,
    "auth_api": AuthApiConnector,
    # TODO(sonnet): register azure_status, google_status, vnet_capacity, quota, database
}
