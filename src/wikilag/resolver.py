"""Entity resolution: (wiki, title) to a shared Wikidata item.

Three tiers, cheapest first:

1. A bounded in-memory LRU, holding both positive and negative results.
   Many edited pages have no Wikidata item, and without negative caching
   every edit to one would cost a request.
2. A SQLite snapshot of every resolution ever fetched. Wikidata changes
   over time, so resolving live on every run would make the output depend
   on when it ran. Replays read the snapshot, which keeps the core
   deterministic; `--offline` forbids the network tier entirely.
3. Batched `wbgetentities` requests, one site per request, with backoff on
   rate limits and replica lag.

Every tier is instrumented from the start: the cache hit rate in the README
is counted here, not estimated.
"""

from __future__ import annotations

import sqlite3
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx
import structlog

from wikilag.config import WikidataConfig

log = structlog.get_logger(__name__)

Key = tuple[str, str]  # (wiki, title)


class RateLimited(Exception):
    """The API asked us to slow down (HTTP 429/503 or a maxlag refusal)."""

    def __init__(self, retry_after: float | None) -> None:
        super().__init__(f"rate limited, retry after {retry_after}")
        self.retry_after = retry_after


class TransientError(Exception):
    """A failure worth retrying: network error or server-side 5xx."""


class ApiError(Exception):
    """The API rejected the request itself. Retrying unchanged won't help."""


class WikidataClient(Protocol):
    def lookup(self, site: str, titles: Sequence[str]) -> dict[str, str | None]:
        """Map each title on `site` to its item id, or None if it has none."""
        ...


def parse_entities(body: dict, site: str, titles: Sequence[str]) -> dict[str, str | None]:
    """Match a wbgetentities response back to the requested titles.

    Found items are keyed by id and carry the title in their sitelink;
    missing pages come back under negative placeholder keys. Titles are
    matched exactly: the API does not normalise case or resolve redirects
    in batch mode, so a redirect title resolves to None.
    """
    result: dict[str, str | None] = dict.fromkeys(titles)
    for entity in body.get("entities", {}).values():
        if "missing" in entity:
            continue
        link = entity.get("sitelinks", {}).get(site)
        if link is not None and link.get("title") in result:
            result[link["title"]] = entity["id"]
    return result


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


class HttpWikidataClient:
    """wbgetentities over HTTP. POST, because 50 CJK titles overflow a URL."""

    def __init__(self, config: WikidataConfig, http: httpx.Client | None = None) -> None:
        self._config = config
        self._http = http or httpx.Client(
            headers={"User-Agent": config.user_agent},
            timeout=config.request_timeout_seconds,
        )

    def lookup(self, site: str, titles: Sequence[str]) -> dict[str, str | None]:
        form = {
            "action": "wbgetentities",
            "format": "json",
            "formatversion": "2",
            "props": "sitelinks",
            "sitefilter": site,
            "sites": site,
            "titles": "|".join(titles),
            "maxlag": str(self._config.maxlag_seconds),
        }
        try:
            response = self._http.post(self._config.api_url, data=form)
        except httpx.TransportError as exc:
            raise TransientError(str(exc)) from exc

        if response.status_code in (429, 503):
            raise RateLimited(_retry_after(response))
        if response.status_code >= 500:
            raise TransientError(f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise ApiError(f"HTTP {response.status_code}")

        body = response.json()
        error = body.get("error")
        if error is not None:
            if error.get("code") == "maxlag":
                raise RateLimited(_retry_after(response))
            raise ApiError(f"{error.get('code')}: {error.get('info')}")
        return parse_entities(body, site, titles)

    def close(self) -> None:
        self._http.close()


class LRUCache:
    """Bounded map with least-recently-used eviction. None is a valid value."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("cache capacity must be at least 1")
        self._capacity = capacity
        self._items: OrderedDict[Key, str | None] = OrderedDict()
        self.evictions = 0

    def get(self, key: Key) -> tuple[bool, str | None]:
        if key not in self._items:
            return False, None
        self._items.move_to_end(key)
        return True, self._items[key]

    def put(self, key: Key, value: str | None) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        if len(self._items) > self._capacity:
            self._items.popitem(last=False)
            self.evictions += 1

    def __len__(self) -> int:
        return len(self._items)


class ResolutionStore:
    """Durable snapshot of fetched resolutions. qid NULL means "no item"."""

    def __init__(self, path: Path | str) -> None:
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS resolutions ("
            " wiki TEXT NOT NULL, title TEXT NOT NULL, qid TEXT,"
            " PRIMARY KEY (wiki, title))"
        )

    def get(self, key: Key) -> tuple[bool, str | None]:
        row = self._db.execute(
            "SELECT qid FROM resolutions WHERE wiki = ? AND title = ?", key
        ).fetchone()
        return (False, None) if row is None else (True, row[0])

    def get_many(self, keys: Sequence[Key], chunk_size: int) -> dict[Key, str | None]:
        """Stored resolutions for the keys present, in one query per chunk.

        Profiling showed per-key SELECTs at 47% of replay time: statement
        overhead, not index lookups. Joining a VALUES list against the
        primary key does the same lookups in a single statement.
        """
        found: dict[Key, str | None] = {}
        for start in range(0, len(keys), chunk_size):
            chunk = keys[start : start + chunk_size]
            placeholders = ",".join("(?, ?)" for _ in chunk)
            parameters = [part for key in chunk for part in key]
            rows = self._db.execute(
                f"WITH wanted(wiki, title) AS (VALUES {placeholders}) "
                "SELECT r.wiki, r.title, r.qid FROM wanted "
                "JOIN resolutions r ON r.wiki = wanted.wiki AND r.title = wanted.title",
                parameters,
            )
            for wiki, title, qid in rows:
                found[(wiki, title)] = qid
        return found

    def put_many(self, items: dict[Key, str | None]) -> None:
        with self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO resolutions (wiki, title, qid) VALUES (?, ?, ?)",
                [(wiki, title, qid) for (wiki, title), qid in items.items()],
            )

    def __len__(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0]

    def close(self) -> None:
        self._db.close()


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """Nearest-rank percentile; None for no data rather than a fake zero."""
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * len(ordered)) - 1))
    return ordered[index]


@dataclass
class ResolverStats:
    references: int = 0  # every (wiki, title) lookup requested by the pipeline
    cache_hits: int = 0
    negative_cache_hits: int = 0
    coalesced: int = 0  # repeat of a key already pending in the same block
    store_hits: int = 0
    offline_unresolved: int = 0
    network_titles: int = 0
    network_requests: int = 0
    retries: int = 0
    rate_limited: int = 0
    api_errors: int = 0
    failed_titles: int = 0  # still unresolved after max_retries; not cached
    resolved_with_item: int = 0
    resolved_without_item: int = 0
    latencies: list[float] = field(default_factory=list, repr=False)

    @property
    def cache_hit_rate(self) -> float:
        return self.cache_hits / self.references if self.references else 0.0

    def summary(self, evictions: int) -> dict:
        return {
            "references": self.references,
            "cache_hits": self.cache_hits,
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "negative_cache_hits": self.negative_cache_hits,
            "coalesced": self.coalesced,
            "store_hits": self.store_hits,
            "offline_unresolved": self.offline_unresolved,
            "network_titles": self.network_titles,
            "network_requests": self.network_requests,
            "retries": self.retries,
            "rate_limited": self.rate_limited,
            "api_errors": self.api_errors,
            "failed_titles": self.failed_titles,
            "evictions": evictions,
            "keys_with_item": self.resolved_with_item,
            "keys_without_item": self.resolved_without_item,
            "lookup_latency_p50_ms": _ms(percentile(self.latencies, 0.5)),
            "lookup_latency_p95_ms": _ms(percentile(self.latencies, 0.95)),
        }


def _ms(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds * 1000, 1)


class Resolver:
    """Resolve blocks of keys through cache, snapshot, then network."""

    def __init__(
        self,
        config: WikidataConfig,
        store: ResolutionStore,
        client: WikidataClient | None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._config = config
        self._store = store
        self._client = client  # None means offline: snapshot only
        self._sleep = sleep
        self._clock = clock
        self.cache = LRUCache(config.cache_size)
        self.stats = ResolverStats()

    def resolve_block(self, keys: Sequence[Key]) -> list[str | None]:
        """Item id (or None) for each key, in order.

        Keys are gathered for the whole block before any lookup so that
        requests can be batched across events.
        """
        known: dict[Key, str | None] = {}
        pending: dict[Key, None] = {}

        for key in keys:
            self.stats.references += 1
            if key in pending:
                self.stats.coalesced += 1
                continue
            hit, value = self.cache.get(key)
            if hit:
                self.stats.cache_hits += 1
                self.stats.negative_cache_hits += value is None
                known[key] = value
            else:
                pending[key] = None

        for key, value in self._resolve_misses(list(pending)).items():
            known[key] = value

        return [known.get(key) for key in keys]

    def _resolve_misses(self, keys: list[Key]) -> dict[Key, str | None]:
        resolved: dict[Key, str | None] = {}
        remote: dict[str, list[str]] = {}
        stored = self._store.get_many(keys, self._config.store_query_chunk)

        for key in keys:
            if key in stored:
                value = stored[key]
                self.stats.store_hits += 1
                self._remember(key, value)
                resolved[key] = value
            elif self._client is None:
                self.stats.offline_unresolved += 1
                resolved[key] = None
            else:
                remote.setdefault(key[0], []).append(key[1])

        for site in sorted(remote):
            titles = remote[site]
            for start in range(0, len(titles), self._config.batch_size):
                batch = titles[start : start + self._config.batch_size]
                fetched = self._fetch(site, batch)
                self._store.put_many({(site, t): q for t, q in fetched.items()})
                for title in batch:
                    key = (site, title)
                    if title in fetched:
                        self._remember(key, fetched[title])
                        resolved[key] = fetched[title]
                    else:
                        self.stats.failed_titles += 1
                        resolved[key] = None
        return resolved

    def _remember(self, key: Key, value: str | None) -> None:
        self.cache.put(key, value)
        if value is None:
            self.stats.resolved_without_item += 1
        else:
            self.stats.resolved_with_item += 1

    def _fetch(self, site: str, titles: list[str]) -> dict[str, str | None]:
        """Look up one batch; titles absent from the result failed for good.

        An ApiError is about the request, not the moment, so the batch is
        bisected until the offending title is isolated. That title is
        recorded as having no item, and the rest still resolve.
        """
        try:
            result = self._with_retry(site, titles)
        except ApiError as exc:
            if len(titles) == 1:
                self.stats.api_errors += 1
                log.warning(
                    "wikidata.api_error", site=site, title=titles[0], error=str(exc)
                )
                return {titles[0]: None}
            middle = len(titles) // 2
            return {
                **self._fetch(site, titles[:middle]),
                **self._fetch(site, titles[middle:]),
            }
        if result is None:
            log.error("wikidata.gave_up", site=site, titles=len(titles))
            return {}
        return result

    def _with_retry(self, site: str, titles: list[str]) -> dict[str, str | None] | None:
        assert self._client is not None
        delay = self._config.backoff_initial_seconds
        for attempt in range(self._config.max_retries + 1):
            started = self._clock()
            try:
                result = self._client.lookup(site, titles)
            except RateLimited as exc:
                self.stats.rate_limited += 1
                wait = exc.retry_after if exc.retry_after is not None else delay
                reason = "rate_limited"
            except TransientError as exc:
                wait = delay
                reason = str(exc)
            else:
                self.stats.latencies.append(self._clock() - started)
                self.stats.network_requests += 1
                self.stats.network_titles += len(titles)
                return result

            if attempt == self._config.max_retries:
                return None
            wait = min(wait, self._config.backoff_max_seconds)
            self.stats.retries += 1
            log.warning(
                "wikidata.retry", site=site, attempt=attempt + 1, wait=wait, reason=reason
            )
            self._sleep(wait)
            delay = min(delay * 2, self._config.backoff_max_seconds)
        return None
