from __future__ import annotations

import httpx

from digest.errors import ParseError
from digest.fetch.base import FetchResult, FetchTask
from digest.fetch.http import HttpFetcher


class ApiFetcher(HttpFetcher):
    """Same transport, retry, rate limit and robots handling as HttpFetcher — it only
    differs in what it makes of the body."""

    def _build_result(self, task: FetchTask, response: httpx.Response) -> FetchResult:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ParseError(f"{task.url} did not return valid JSON: {exc}") from exc
        return FetchResult(
            task=task,
            status=response.status_code,
            text=response.text,
            json=payload,
            from_cache=False,
        )
