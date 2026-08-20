from __future__ import annotations

from time import monotonic, sleep
from types import TracebackType
from typing import Self
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import structlog

from digest.config import Config
from digest.errors import FetchError
from digest.fetch.base import FetchResult, FetchTask

log = structlog.get_logger()

# A Retry-After longer than this is not worth blocking the run for: skip the source and
# let the next run pick it up (§6.4).
_MAX_RETRY_AFTER_SECONDS = 60

_ROBOTS_DISALLOW_ALL = "User-agent: *\nDisallow: /"


def _parse_robots(text: str) -> RobotFileParser:
    parser = RobotFileParser()
    parser.parse(text.splitlines())
    return parser


class HttpFetcher:
    """One instance per run. It owns the shared client, the per-source spacing and the
    robots.txt cache — all three are meaningless if every source builds its own."""

    def __init__(self, config: Config, client: httpx.Client | None = None) -> None:
        self._config = config
        self._client = client or httpx.Client(
            headers={"User-Agent": config.fetch.user_agent},
            timeout=config.fetch.timeout_seconds,
            follow_redirects=True,
        )
        self._last_request_at: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def fetch(
        self,
        task: FetchTask,
        *,
        source_id: str,
        rate_limit_seconds: float | None = None,
        etag: str | None = None,
    ) -> FetchResult:
        if self._config.fetch.respect_robots_txt and not self._robots_allows(task.url):
            raise FetchError(f"robots.txt disallows {task.url} for {self._config.fetch.user_agent}")

        self._respect_rate_limit(source_id, rate_limit_seconds)
        response = self._request_with_retries(task, etag)
        if response.status_code == httpx.codes.NOT_MODIFIED:
            return FetchResult(
                task=task,
                status=response.status_code,
                text="",
                json=None,
                from_cache=True,
            )
        return self._build_result(task, response)

    def _build_result(self, task: FetchTask, response: httpx.Response) -> FetchResult:
        return FetchResult(
            task=task,
            status=response.status_code,
            text=response.text,
            json=None,
            from_cache=False,
        )

    def _request_with_retries(self, task: FetchTask, etag: str | None) -> httpx.Response:
        headers = dict(task.headers)
        if etag:
            headers["If-None-Match"] = etag

        attempts = self._config.fetch.max_retries + 1
        for attempt in range(1, attempts + 1):
            last_attempt = attempt == attempts
            try:
                response = self._send(task, headers)
            except httpx.HTTPError as exc:
                if last_attempt:
                    raise FetchError(f"{task.url} failed after {attempts} attempts: {exc}") from exc
                log.warning("fetch_retry", url=task.url, attempt=attempt, error=str(exc))
                sleep(self._backoff_seconds(attempt))
                continue

            status = response.status_code
            if status == httpx.codes.TOO_MANY_REQUESTS:
                wait = self._retry_after_seconds(response)
                if wait is None or last_attempt:
                    raise FetchError(f"{task.url} rate limited (429), skipping the source")
                log.warning("fetch_rate_limited", url=task.url, retry_after=wait)
                sleep(wait)
                continue
            if status >= httpx.codes.INTERNAL_SERVER_ERROR:
                if last_attempt:
                    raise FetchError(f"{task.url} returned {status} after {attempts} attempts")
                log.warning("fetch_retry", url=task.url, attempt=attempt, status=status)
                sleep(self._backoff_seconds(attempt))
                continue
            if status >= httpx.codes.BAD_REQUEST:
                raise FetchError(f"{task.url} returned {status}")
            return response

        raise FetchError(f"{task.url} exhausted {attempts} attempts")

    def _send(self, task: FetchTask, headers: dict[str, str]) -> httpx.Response:
        return self._client.request(
            task.method,
            task.url,
            headers=headers,
            params=task.params,
            json=task.json_body,
        )

    def _backoff_seconds(self, attempt: int) -> float:
        return self._config.fetch.backoff_base_seconds * (2 ** (attempt - 1))

    def _retry_after_seconds(self, response: httpx.Response) -> float | None:
        """None means "cannot honour this" — an absent, unparseable or too distant
        Retry-After all end the same way: skip the source (§6.4)."""
        raw = response.headers.get("Retry-After")
        if raw is None:
            return None
        try:
            seconds = float(raw.strip())
        except ValueError:
            log.warning("retry_after_unparseable", value=raw)
            return None
        if seconds < 0 or seconds > _MAX_RETRY_AFTER_SECONDS:
            return None
        return seconds

    def _respect_rate_limit(self, source_id: str, rate_limit_seconds: float | None) -> None:
        limit = (
            rate_limit_seconds
            if rate_limit_seconds is not None
            else self._config.fetch.default_rate_limit_seconds
        )
        last = self._last_request_at.get(source_id)
        if last is not None:
            remaining = limit - (monotonic() - last)
            if remaining > 0:
                sleep(remaining)
        self._last_request_at[source_id] = monotonic()

    def _robots_allows(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            self._robots[origin] = self._load_robots(origin)
        parser = self._robots[origin]
        if parser is None:
            return True
        return parser.can_fetch(self._config.fetch.user_agent, url)

    def _load_robots(self, origin: str) -> RobotFileParser | None:
        """None means "no robots.txt, everything allowed". RFC 9309: a 4xx means allow all,
        while unreachable or 5xx means assume disallowed rather than crawl blind."""
        try:
            response = self._client.get(f"{origin}/robots.txt")
        except httpx.HTTPError as exc:
            log.warning("robots_unreachable", origin=origin, error=str(exc))
            return _parse_robots(_ROBOTS_DISALLOW_ALL)
        if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
            log.warning("robots_server_error", origin=origin, status=response.status_code)
            return _parse_robots(_ROBOTS_DISALLOW_ALL)
        if response.status_code >= httpx.codes.BAD_REQUEST:
            return None
        return _parse_robots(response.text)
