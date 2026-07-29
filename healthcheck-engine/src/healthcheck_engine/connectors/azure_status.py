from __future__ import annotations

import time
import xml.etree.ElementTree as ET

import httpx

from ..schema import CheckResult, CheckStatus, ErrorType
from .base import Connector

AZURE_STATUS_FEED_URL = "https://azurestatuscdn.azureedge.net/en-us/status/feed/"


def _local_tag(tag: str) -> str:
    """Strip an XML namespace prefix, e.g. '{http://www.w3.org/2005/Atom}entry' -> 'entry'."""
    return tag.rsplit("}", 1)[-1]


def _entry_title(entry: ET.Element) -> str | None:
    for child in entry:
        if _local_tag(child.tag) == "title" and child.text:
            return child.text
    return None


class AzureStatusConnector(Connector):
    """params:
    services: list[str]    (required) — substrings matched case-insensitively against
                            each feed entry's title.

    Fetches Azure's public status feed and looks for entries whose title mentions one
    of the configured services.

    Format handling: Azure's feed at this URL has historically been Atom, but the brief
    flags it as "Atom/RSS" and not as stable as Google's JSON. We don't hard-code a
    namespace; instead we walk the whole tree and treat any element whose *local* tag
    (namespace stripped) is "entry" (Atom) or "item" (RSS) as one status entry, then
    take its first "title" child. This is deliberately format-tolerant.

    Severity heuristic: Azure's public feed doesn't reliably expose machine-parseable
    severity/impact without fetching each entry's own detail page (out of scope here),
    so any title match is reported as DEGRADED — never DOWN — with the matched titles
    in metadata so a human can judge severity.

    Fail-soft: if the feed can't be fetched or parsed, that's UNKNOWN/PARSE (or
    TIMEOUT/CONNECTION for network-layer failures) — a status feed we couldn't read is
    not evidence of health, so it must never be reported as OK. Only a feed that
    fetched and parsed successfully, with no matching entries, is a genuine OK.
    """

    async def check(self) -> CheckResult:
        p = self.cfg.params
        services: list[str] = p["services"]

        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.cfg.timeout_seconds) as client:
                resp = await client.get(AZURE_STATUS_FEED_URL)
            latency = (time.perf_counter() - start) * 1000
        except httpx.TimeoutException:
            return CheckResult(source=self.name, check_type=self.cfg.type,
                               status=CheckStatus.DOWN, error_type=ErrorType.TIMEOUT,
                               latency_ms=(time.perf_counter() - start) * 1000,
                               metadata={"url": AZURE_STATUS_FEED_URL})
        except httpx.TransportError as exc:
            return CheckResult(source=self.name, check_type=self.cfg.type,
                               status=CheckStatus.ERROR, error_type=ErrorType.CONNECTION,
                               error_detail=str(exc), metadata={"url": AZURE_STATUS_FEED_URL})

        if resp.status_code != 200:
            # A non-200 means we couldn't actually fetch the feed content, which the
            # brief treats the same as "feed unreachable" -> UNKNOWN/PARSE, not a
            # confident DOWN/DEGRADED verdict about Azure services.
            return CheckResult(source=self.name, check_type=self.cfg.type,
                               status=CheckStatus.UNKNOWN, latency_ms=latency,
                               error_type=ErrorType.PARSE,
                               error_detail=f"HTTP {resp.status_code}",
                               metadata={"url": AZURE_STATUS_FEED_URL,
                                         "status_code": resp.status_code})

        try:
            root = ET.fromstring(resp.content)
            entries = [el for el in root.iter() if _local_tag(el.tag) in ("entry", "item")]
            matched_titles: list[str] = []
            for entry in entries:
                title = _entry_title(entry)
                if not title:
                    continue
                title_lower = title.lower()
                if any(service.lower() in title_lower for service in services):
                    matched_titles.append(title)

            if not matched_titles:
                return CheckResult(source=self.name, check_type=self.cfg.type,
                                   status=CheckStatus.OK, latency_ms=latency,
                                   metadata={"url": AZURE_STATUS_FEED_URL})

            return CheckResult(source=self.name, check_type=self.cfg.type,
                               status=CheckStatus.DEGRADED, latency_ms=latency,
                               metadata={"url": AZURE_STATUS_FEED_URL,
                                         "matched_titles": matched_titles})
        except (ET.ParseError, KeyError, TypeError, ValueError) as exc:
            # Broad catch is deliberate: this is an unversioned public feed whose exact
            # shape can change without notice — fail soft, never raise.
            return CheckResult(source=self.name, check_type=self.cfg.type,
                               status=CheckStatus.UNKNOWN, latency_ms=latency,
                               error_type=ErrorType.PARSE, error_detail=str(exc)[:500],
                               metadata={"url": AZURE_STATUS_FEED_URL})
