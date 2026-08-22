from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import structlog

from digest.config import Config
from digest.errors import ParseError
from digest.fetch.base import FetchResult, FetchTask
from digest.models import RawEvent

log = structlog.get_logger()

_EVENT_BASE_URL = "https://cooltix.hu/event"

# `countryCode: HU` already drops Austria and Serbia, but Cooltix is nationwide inside
# Hungary: of 130 dated on-site records in the saved page budget, 47 are not Budapest
# (44 name another settlement, 3 name none).
# §7.6 is the authoritative geographic rule and now has `city` to apply it — this cut
# stays because that section says it should: don't carry through what you would discard.
_CITY = "budapest"

# The one query this source sends. `status: LIVE` excludes drafts and finished events,
# and `orderBy: startDate_ASC` is what makes a bounded page count enough: the 14-day
# horizon is a prefix of the result set, so the plugin never has to walk the whole
# catalogue. Written out in full rather than assembled, so what is sent is what is read.
_QUERY = """
query BudapestEvents($count: Int!, $cursor: String) {
  events(first: $count, after: $cursor, status: LIVE, countryCode: HU, orderBy: startDate_ASC) {
    pageInfo { endCursor hasNextPage }
    edges { node { id name summary startDate endDate timeZoneId isOnline
      category { name }
      coverImage { url }
      venue { name address { city addressLine postalCode formatted } coordinates { latitude longitude } } } }
  }
}"""


class CooltixSource:
    """cooltix.hu's GraphQL backend (§6.1 step 2). The site itself is a Next.js app whose
    `pageProps` ship empty — every listing is fetched client-side from
    `api.cooltix.com/graphql`, which answers unauthenticated and has introspection on."""

    def __init__(self, spec: dict[str, Any], config: Config) -> None:
        self.id: str = spec["id"]
        self.name: str = spec.get("name", "Cooltix")
        self.enabled: bool = bool(spec.get("enabled", True))
        self.priority: int = int(spec.get("priority", 30))
        self.fetcher: str = spec.get("fetcher", "api")
        self.rate_limit_seconds: float = float(
            spec.get("rate_limit_seconds", config.fetch.default_rate_limit_seconds)
        )
        listing = spec.get("listing") or {}
        self._listing_urls: list[str] = list(listing.get("urls") or [])
        pagination = listing.get("pagination") or {}
        self._page_size: int = int(pagination.get("page_size", 500))
        self._max_pages: int = int(pagination.get("max", 3))
        # Set by parse() after each page and read by discover() before yielding the next
        # one — the same side-channel DeclarativeSource.discover() documents, and it works
        # for the same reason: cli.py's _fetch_source drives discover()/parse() in one
        # sequential loop.
        self._cursor: str | None = None
        self._has_next_page = True

    def discover(self) -> Iterable[FetchTask]:
        """Cursor pagination, so page N+1's request body is only knowable after page N has
        been parsed. `_max_pages` is a hard stop rather than "page until the dates leave
        the horizon", because the horizon is `now`-relative and reading the clock here
        would make the fixture test depend on the day it runs."""
        if not self._listing_urls:
            log.warning("no_listing_urls", source_id=self.id)
            return
        for url in self._listing_urls:
            self._cursor = None
            self._has_next_page = True
            for page in range(self._max_pages):
                if not self._has_next_page:
                    break
                variables: dict[str, Any] = {"count": self._page_size}
                if self._cursor is not None:
                    variables["cursor"] = self._cursor
                yield FetchTask(
                    url=url,
                    method="POST",
                    json_body={"query": _QUERY, "variables": variables},
                )
                if self._has_next_page and page == self._max_pages - 1:
                    log.info(
                        "pagination_capped",
                        source_id=self.id,
                        max_pages=self._max_pages,
                        page_size=self._page_size,
                    )

    def parse(self, result: FetchResult) -> Iterable[RawEvent]:
        payload = result.json
        if not isinstance(payload, dict):
            raise ParseError(f"{self.id}: expected a JSON object, got {type(payload).__name__}")
        if payload.get("errors"):
            raise ParseError(f"{self.id}: GraphQL errors {payload['errors']}")
        connection = ((payload.get("data") or {}).get("events")) or {}
        page_info = connection.get("pageInfo") or {}
        self._has_next_page = bool(page_info.get("hasNextPage"))
        self._cursor = page_info.get("endCursor")

        edges = connection.get("edges")
        if not isinstance(edges, list):
            raise ParseError(f"{self.id}: events.edges missing or not a list")
        for index, edge in enumerate(edges):
            node = (edge or {}).get("node")
            event = self._parse_node(index, node)
            if event is not None:
                yield event

    def _parse_node(self, index: int, node: Any) -> RawEvent | None:
        if not isinstance(node, dict):
            self._skip(str(index), "node is not an object")
            return None

        key = str(node.get("id") or index)
        title = str(node.get("name") or "").strip()
        if not title:
            self._skip(key, "missing name")
            return None

        # NO DATE AT ALL — not "no clock". Vouchers, gift cards and permanent exhibitions
        # are sold as events but have no start whatsoever: they are not "what's on in the
        # next 14 days", and `orderBy: startDate_ASC` sorts every one of them to the front,
        # so they are the bulk of page 1 (369 of 500 nodes in the saved response).
        #
        # A record with a date but no clock is NOT this case and must never be dropped:
        # §7.1 reads a bare date as `start_time_known: False` and the whole pipeline
        # handles it. Cooltix happens to publish a full timestamp whenever it publishes a
        # start at all (131 of 131), so that shape never reaches here — the name
        # `skipped_undated` invited the confusion, which is why it is now spelled out.
        start_raw = node.get("startDate")
        if not start_raw:
            log.info("skipped_no_start_date", source_id=self.id, key=key, title=title[:60])
            return None

        if node.get("isOnline"):
            log.info("skipped_online", source_id=self.id, key=key)
            return None

        venue = node.get("venue") or {}
        address = venue.get("address") or {}
        city = str(address.get("city") or "").strip().casefold()
        if city != _CITY:
            log.info("skipped_outside_budapest", source_id=self.id, key=key, city=city or None)
            return None

        postal_code = str(address.get("postalCode") or "").strip() or None
        coordinates = venue.get("coordinates") or {}

        return RawEvent(
            source_id=self.id,
            source_event_key=key,
            title=title,
            url=f"{_EVENT_BASE_URL}/{key}",
            # `summary` is the short plain-text blurb the cards show; `description` is the
            # full HTML body, and §7.1 unescapes and truncates but does not strip tags.
            description=str(node.get("summary") or "").strip() or None,
            start_raw=str(start_raw),
            end_raw=str(node.get("endDate") or "") or None,
            venue_name=str(venue.get("name") or "").strip() or None,
            # As published, in its own case — §7.1 canonicalizes it and §7.6 compares it.
            # The source-level cut above means only Budapest ever gets here, but §7.6 is
            # the authoritative rule and needs this field to apply it; it is also what
            # survives a dedup merge into a base record that knows no settlement (§7.2).
            city=str(address.get("city") or "").strip() or None,
            address_raw=str(address.get("formatted") or "").strip() or None,
            postal_code=postal_code,
            # Real per-venue coordinates, which is what §7.7's `home`/`distance_km`
            # proximity bonus needs and most sources never publish.
            lat=_coordinate(coordinates, "latitude"),
            lon=_coordinate(coordinates, "longitude"),
            # Not in this query: pricing lives under `productList`, one nested connection
            # per event, which would turn a 3-request source into a per-event crawl.
            price_raw=None,
            image_url=str((node.get("coverImage") or {}).get("url") or "").strip() or None,
            native_category=_native_category(node),
        )

    def _skip(self, key: str, reason: str) -> None:
        log.warning("record_skipped", source_id=self.id, key=key, reason=reason)


def _coordinate(coordinates: dict[str, Any], key: str) -> float | None:
    value = coordinates.get(key)
    return float(value) if isinstance(value, int | float) else None


def _native_category(node: dict[str, Any]) -> str | None:
    """Cooltix spells its categories "Concert", "Movie", "Party"; `config.yaml`'s
    `native_types` are lowercase, and §7.5 compares them as exact strings."""
    name = str((node.get("category") or {}).get("name") or "").strip()
    return name.casefold() or None


def build(spec: dict[str, Any], config: Config) -> CooltixSource:
    return CooltixSource(spec, config)
