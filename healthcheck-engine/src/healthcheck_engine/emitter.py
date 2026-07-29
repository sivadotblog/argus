"""The ONLY module allowed to print to stdout. OTEL captures stdout → Splunk."""
from __future__ import annotations

import json
import sys

from pydantic import BaseModel

_MAX_ERROR_DETAIL = 500


def emit(event: BaseModel) -> None:
    data = event.model_dump(mode="json")
    detail = data.get("error_detail")
    if detail:
        data["error_detail"] = detail[:_MAX_ERROR_DETAIL]
    # one JSON object per line, no pretty-printing — Splunk-friendly
    print(json.dumps(data, separators=(",", ":")), file=sys.stdout, flush=True)
