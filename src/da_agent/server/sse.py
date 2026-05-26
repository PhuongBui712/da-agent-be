"""SSE event formatter — `event: <type>\\ndata: <json>\\n\\n`."""

from __future__ import annotations

import json
from typing import Any


def format_event(event: dict[str, Any]) -> str:
    type_ = event.get("type", "message")
    data = json.dumps(event, default=str, ensure_ascii=False)
    return f"event: {type_}\ndata: {data}\n\n"
