from __future__ import annotations

import httpx
import pytest
import respx

from digest.config import Config, FetchConfig
from digest.errors import FetchError, ParseError
from digest.fetch.api import ApiFetcher
from digest.fetch.base import FetchTask
from digest.fetch.http import HttpFetcher

URL = "https://example.com/list"
ROBOTS = "https://example.com/robots.txt"
USER_AGENT = "budapest-event-digest/1.0 (+https://example.invalid/contact)"


def make_config(*, respect_robots_txt: bool = False, **overrides: object) -> Config:
    return Config(
        fetch=FetchConfig(
            user_agent=USER_AGENT,
            respect_robots_txt=respect_robots_txt,
            **overrides,
        )
    )


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Every wait the fetcher performs, in order — and nothing actually waits."""
    recorded: list[float] = []
    monkeypatch.setattr("digest.fetch.http.sleep", recorded.append)
    return recorded


@respx.mock
def test_retries_5xx_then_succeeds(sleeps: list[float]) -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(500),
            httpx.Response(200, text="ok"),
        ]
    )

    result = HttpFetcher(make_config()).fetch(FetchTask(url=URL), source_id="demo")

    assert result.status == 200
    assert result.text == "ok"
    assert result.from_cache is False
    assert route.call_count == 3
    assert sleeps == [2, 4]  # exponential: base * 2**(attempt - 1)


@respx.mock
def test_gives_up_after_max_retries(sleeps: list[float]) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(503))

    with pytest.raises(FetchError, match="503"):
        HttpFetcher(make_config(max_retries=2)).fetch(FetchTask(url=URL), source_id="demo")

    assert route.call_count == 3  # the first attempt plus max_retries


@respx.mock
def test_network_error_is_retried(sleeps: list[float]) -> None:
    route = respx.get(URL).mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(200, text="ok")]
    )

    result = HttpFetcher(make_config()).fetch(FetchTask(url=URL), source_id="demo")

    assert result.text == "ok"
    assert route.call_count == 2


@respx.mock
def test_4xx_is_never_retried(sleeps: list[float]) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(404))

    with pytest.raises(FetchError, match="404"):
        HttpFetcher(make_config()).fetch(FetchTask(url=URL), source_id="demo")

    assert route.call_count == 1
    assert sleeps == []


@respx.mock
def test_429_waits_for_retry_after_then_retries(sleeps: list[float]) -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, text="ok"),
        ]
    )

    result = HttpFetcher(make_config()).fetch(FetchTask(url=URL), source_id="demo")

    assert result.text == "ok"
    assert route.call_count == 2
    assert sleeps == [2]


@respx.mock
@pytest.mark.parametrize("headers", [{}, {"Retry-After": "120"}, {"Retry-After": "soon"}])
def test_429_without_a_usable_retry_after_skips_the_source(
    sleeps: list[float], headers: dict[str, str]
) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(429, headers=headers))

    with pytest.raises(FetchError, match="rate limited"):
        HttpFetcher(make_config()).fetch(FetchTask(url=URL), source_id="demo")

    assert route.call_count == 1
    assert sleeps == []


@respx.mock
def test_etag_produces_a_conditional_request_and_304_is_cached(sleeps: list[float]) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(304))

    result = HttpFetcher(make_config()).fetch(FetchTask(url=URL), source_id="demo", etag='"abc"')

    assert route.calls.last.request.headers["If-None-Match"] == '"abc"'
    assert result.from_cache is True
    assert result.status == 304
    assert result.text == ""
    assert result.json is None


@respx.mock
def test_rate_limit_spaces_two_calls_to_the_same_source(sleeps: list[float]) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))
    fetcher = HttpFetcher(make_config())

    fetcher.fetch(FetchTask(url=URL), source_id="demo", rate_limit_seconds=3)
    assert sleeps == []  # nothing to wait for on the first request

    fetcher.fetch(FetchTask(url=URL), source_id="demo", rate_limit_seconds=3)
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= 3


@respx.mock
def test_rate_limit_is_per_source(sleeps: list[float]) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))
    fetcher = HttpFetcher(make_config())

    fetcher.fetch(FetchTask(url=URL), source_id="one", rate_limit_seconds=3)
    fetcher.fetch(FetchTask(url=URL), source_id="two", rate_limit_seconds=3)

    assert sleeps == []


@respx.mock
def test_robots_disallow_blocks_the_request(sleeps: list[float]) -> None:
    respx.get(ROBOTS).mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private")
    )
    blocked = respx.get("https://example.com/private/x").mock(
        return_value=httpx.Response(200, text="should never be read")
    )

    with pytest.raises(FetchError, match="robots.txt"):
        HttpFetcher(make_config(respect_robots_txt=True)).fetch(
            FetchTask(url="https://example.com/private/x"), source_id="demo"
        )

    assert blocked.call_count == 0


@respx.mock
def test_robots_is_fetched_once_per_host(sleeps: list[float]) -> None:
    robots = respx.get(ROBOTS).mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private")
    )
    respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))
    fetcher = HttpFetcher(make_config(respect_robots_txt=True))

    fetcher.fetch(FetchTask(url=URL), source_id="demo")
    fetcher.fetch(FetchTask(url=URL), source_id="demo")

    assert robots.call_count == 1


@respx.mock
def test_missing_robots_allows_everything(sleeps: list[float]) -> None:
    respx.get(ROBOTS).mock(return_value=httpx.Response(404))
    respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))

    result = HttpFetcher(make_config(respect_robots_txt=True)).fetch(
        FetchTask(url=URL), source_id="demo"
    )

    assert result.text == "ok"


@respx.mock
def test_unreadable_robots_blocks_the_host(sleeps: list[float]) -> None:
    respx.get(ROBOTS).mock(return_value=httpx.Response(503))
    respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))

    with pytest.raises(FetchError, match="robots.txt"):
        HttpFetcher(make_config(respect_robots_txt=True)).fetch(
            FetchTask(url=URL), source_id="demo"
        )


@respx.mock
def test_robots_check_is_skipped_when_disabled(sleeps: list[float]) -> None:
    # No robots.txt route is registered: requesting it would fail the test outright.
    respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))

    result = HttpFetcher(make_config(respect_robots_txt=False)).fetch(
        FetchTask(url=URL), source_id="demo"
    )

    assert result.text == "ok"


@respx.mock
def test_user_agent_comes_from_config(sleeps: list[float]) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))

    HttpFetcher(make_config()).fetch(FetchTask(url=URL), source_id="demo")

    assert route.calls.last.request.headers["User-Agent"] == USER_AGENT


@respx.mock
def test_task_headers_and_params_reach_the_request(sleeps: list[float]) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))
    task = FetchTask(url=URL, headers={"X-Test": "1"}, params={"page": 2})

    HttpFetcher(make_config()).fetch(task, source_id="demo")

    request = route.calls.last.request
    assert request.headers["X-Test"] == "1"
    assert request.url.params["page"] == "2"


@respx.mock
def test_api_fetcher_parses_json(sleeps: list[float]) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={"events": [{"id": 1}]}))

    result = ApiFetcher(make_config()).fetch(FetchTask(url=URL), source_id="demo")

    assert result.json == {"events": [{"id": 1}]}
    assert result.from_cache is False


@respx.mock
def test_api_fetcher_raises_parse_error_on_invalid_json(sleeps: list[float]) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>nope</html>"))

    with pytest.raises(ParseError, match="valid JSON"):
        ApiFetcher(make_config()).fetch(FetchTask(url=URL), source_id="demo")


@respx.mock
def test_api_fetcher_does_not_parse_a_304(sleeps: list[float]) -> None:
    respx.get(URL).mock(return_value=httpx.Response(304))

    result = ApiFetcher(make_config()).fetch(FetchTask(url=URL), source_id="demo", etag='"abc"')

    assert result.from_cache is True
    assert result.json is None


@respx.mock
def test_fetcher_closes_its_client(sleeps: list[float]) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))

    with HttpFetcher(make_config()) as fetcher:
        fetcher.fetch(FetchTask(url=URL), source_id="demo")

    assert fetcher._client.is_closed
