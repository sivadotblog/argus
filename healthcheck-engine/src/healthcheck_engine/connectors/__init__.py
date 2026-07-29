from __future__ import annotations

from .auth_api import AuthApiConnector
from .azure_status import AzureStatusConnector
from .base import Connector
from .google_status import GoogleStatusConnector
from .public_api import PublicApiConnector

REGISTRY: dict[str, type[Connector]] = {
    "public_api": PublicApiConnector,
    "auth_api": AuthApiConnector,
    "azure_status": AzureStatusConnector,
    "google_status": GoogleStatusConnector,
    # TODO(sonnet): register vnet_capacity, quota, database
}
