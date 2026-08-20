from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FetchTask:
    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] | None = None
    json_body: dict | None = None


@dataclass(frozen=True)
class FetchResult:
    task: FetchTask
    status: int
    text: str
    json: Any | None
    from_cache: bool
