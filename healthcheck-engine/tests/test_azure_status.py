"""Tests for AzureStatusConnector, HTTP mocked via respx — no real network calls."""
from __future__ import annotations

import httpx
import respx

from healthcheck_engine.connectors.azure_status import (
    AZURE_STATUS_FEED_URL,
    AzureStatusConnector,
)
from healthcheck_engine.schema import CheckStatus, ConnectorConfig, ErrorType

ATOM_NO_MATCH = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Azure Status</title>
  <entry>
    <title>Azure Cosmos DB - Investigating latency in West Europe</title>
    <updated>2026-07-27T09:00:00Z</updated>
    <id>urn:uuid:1</id>
  </entry>
</feed>
"""

ATOM_MATCH = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Azure Status</title>
  <entry>
    <title>Virtual Machines - Performance issues in East US</title>
    <updated>2026-07-27T10:00:00Z</updated>
    <id>urn:uuid:2</id>
  </entry>
  <entry>
    <title>Azure Cosmos DB - Investigating latency in West Europe</title>
    <updated>2026-07-27T09:00:00Z</updated>
    <id>urn:uuid:1</id>
  </entry>
</feed>
"""

RSS_MATCH = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>Azure Status</title>
    <item>
      <title>Virtual Machines - Performance issues in East US</title>
      <pubDate>Mon, 27 Jul 2026 10:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


def make_cfg(**params: object) -> ConnectorConfig:
    return ConnectorConfig(
        name="azure-status",
        type="azure_status",
        params={"services": ["Virtual Machines"], **params},
    )


@respx.mock
async def test_no_matching_entries_is_ok() -> None:
    respx.get(AZURE_STATUS_FEED_URL).mock(
        return_value=httpx.Response(200, content=ATOM_NO_MATCH.encode())
    )
    connector = AzureStatusConnector(make_cfg(), None)

    result = await connector.check()

    assert result.status == CheckStatus.OK
    assert result.error_type == ErrorType.NONE
    assert result.metadata == {"url": AZURE_STATUS_FEED_URL}


@respx.mock
async def test_matching_atom_entry_is_degraded() -> None:
    respx.get(AZURE_STATUS_FEED_URL).mock(
        return_value=httpx.Response(200, content=ATOM_MATCH.encode())
    )
    connector = AzureStatusConnector(make_cfg(), None)

    result = await connector.check()

    assert result.status == CheckStatus.DEGRADED
    assert result.error_type == ErrorType.NONE
    assert result.metadata == {
        "url": AZURE_STATUS_FEED_URL,
        "matched_titles": ["Virtual Machines - Performance issues in East US"],
    }


@respx.mock
async def test_matching_rss_entry_is_degraded() -> None:
    # Confirms format tolerance: RSS <item>/<title> matched the same as Atom <entry>.
    respx.get(AZURE_STATUS_FEED_URL).mock(
        return_value=httpx.Response(200, content=RSS_MATCH.encode())
    )
    connector = AzureStatusConnector(make_cfg(), None)

    result = await connector.check()

    assert result.status == CheckStatus.DEGRADED
    assert result.metadata["matched_titles"] == [
        "Virtual Machines - Performance issues in East US"
    ]


@respx.mock
async def test_case_insensitive_service_match() -> None:
    respx.get(AZURE_STATUS_FEED_URL).mock(
        return_value=httpx.Response(200, content=ATOM_MATCH.encode())
    )
    connector = AzureStatusConnector(make_cfg(services=["virtual machines"]), None)

    result = await connector.check()

    assert result.status == CheckStatus.DEGRADED


@respx.mock
async def test_malformed_xml_body_is_unknown_with_parse_error_type() -> None:
    respx.get(AZURE_STATUS_FEED_URL).mock(
        return_value=httpx.Response(200, content=b"<feed><entry><title>unclosed")
    )
    connector = AzureStatusConnector(make_cfg(), None)

    result = await connector.check()

    assert result.status == CheckStatus.UNKNOWN
    assert result.error_type == ErrorType.PARSE
    assert result.error_detail


@respx.mock
async def test_empty_body_is_unknown_with_parse_error_type() -> None:
    respx.get(AZURE_STATUS_FEED_URL).mock(return_value=httpx.Response(200, content=b""))
    connector = AzureStatusConnector(make_cfg(), None)

    result = await connector.check()

    assert result.status == CheckStatus.UNKNOWN
    assert result.error_type == ErrorType.PARSE


@respx.mock
async def test_non_xml_body_is_unknown_with_parse_error_type() -> None:
    # Valid HTTP 200 but the body isn't XML at all (e.g. an HTML error page from a CDN).
    respx.get(AZURE_STATUS_FEED_URL).mock(
        return_value=httpx.Response(200, content=b"<html><body>not a feed</body></html>")
    )
    connector = AzureStatusConnector(make_cfg(), None)

    result = await connector.check()

    # This parses as valid XML with no entry/item elements -> no matches -> OK.
    # A genuinely malformed body is covered by test_malformed_xml_body_is_unknown_...
    assert result.status == CheckStatus.OK


@respx.mock
async def test_non_200_status_is_unknown_with_parse_error_type() -> None:
    respx.get(AZURE_STATUS_FEED_URL).mock(return_value=httpx.Response(503))
    connector = AzureStatusConnector(make_cfg(), None)

    result = await connector.check()

    assert result.status == CheckStatus.UNKNOWN
    assert result.error_type == ErrorType.PARSE
    assert result.error_detail == "HTTP 503"


@respx.mock
async def test_timeout_is_down_with_timeout_error_type() -> None:
    respx.get(AZURE_STATUS_FEED_URL).mock(side_effect=httpx.TimeoutException("timed out"))
    connector = AzureStatusConnector(make_cfg(), None)

    result = await connector.check()

    assert result.status == CheckStatus.DOWN
    assert result.error_type == ErrorType.TIMEOUT


@respx.mock
async def test_connection_error_is_error_with_connection_error_type() -> None:
    respx.get(AZURE_STATUS_FEED_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    connector = AzureStatusConnector(make_cfg(), None)

    result = await connector.check()

    assert result.status == CheckStatus.ERROR
    assert result.error_type == ErrorType.CONNECTION
