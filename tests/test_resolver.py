import json
import socket

import httpx
import pytest

from wikilag.config import WikidataConfig
from wikilag.resolver import (
    ApiError,
    HttpWikidataClient,
    LRUCache,
    RateLimited,
    ResolutionStore,
    Resolver,
    TransientError,
    parse_entities,
    percentile,
)

# Trimmed from a real wbgetentities response (enwiki, formatversion 2).
REAL_RESPONSE = {
    "entities": {
        "-1": {"site": "enwiki", "title": "UK", "missing": ""},
        "-2": {"site": "enwiki", "title": "No such page zzqx123", "missing": ""},
        "Q1490": {
            "type": "item",
            "id": "Q1490",
            "sitelinks": {"enwiki": {"site": "enwiki", "title": "Tokyo", "badges": []}},
        },
    },
    "success": 1,
}


def make_config(**overrides) -> WikidataConfig:
    values = {
        "api_url": "https://wikidata.invalid/w/api.php",
        "user_agent": "wikilag-tests",
        "batch_size": 50,
        "cache_size": 100,
        "store_path": ":memory:",
        "request_timeout_seconds": 1.0,
        "maxlag_seconds": 5,
        "max_retries": 3,
        "backoff_initial_seconds": 1.0,
        "backoff_max_seconds": 8.0,
        "block_events": 100,
    }
    values.update(overrides)
    return WikidataConfig(**values)


class StubClient:
    """Answers from a fixed table and records every request."""

    def __init__(self, items=None, failures=None):
        self.items = items or {}
        self.failures = list(failures or [])
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def lookup(self, site, titles):
        self.calls.append((site, tuple(titles)))
        if self.failures:
            raise self.failures.pop(0)
        return {title: self.items.get((site, title)) for title in titles}


def make_resolver(client, **overrides):
    sleeps: list[float] = []
    resolver = Resolver(
        make_config(**overrides),
        ResolutionStore(":memory:"),
        client,
        sleep=sleeps.append,
        clock=lambda: 0.0,
    )
    return resolver, sleeps


# --- parsing -----------------------------------------------------------------


def test_parse_matches_found_and_missing_titles():
    titles = ["Tokyo", "UK", "No such page zzqx123"]
    assert parse_entities(REAL_RESPONSE, "enwiki", titles) == {
        "Tokyo": "Q1490",
        "UK": None,  # a redirect: batch mode does not follow it
        "No such page zzqx123": None,
    }


# --- LRU cache ---------------------------------------------------------------


def test_lru_evicts_least_recently_used_under_pressure():
    cache = LRUCache(2)
    cache.put(("enwiki", "A"), "Q1")
    cache.put(("enwiki", "B"), "Q2")
    cache.get(("enwiki", "A"))  # A is now more recent than B
    cache.put(("enwiki", "C"), "Q3")

    assert cache.get(("enwiki", "B")) == (False, None)
    assert cache.get(("enwiki", "A")) == (True, "Q1")
    assert len(cache) == 2
    assert cache.evictions == 1


def test_lru_distinguishes_cached_none_from_absent():
    cache = LRUCache(2)
    cache.put(("enwiki", "No item"), None)
    assert cache.get(("enwiki", "No item")) == (True, None)
    assert cache.get(("enwiki", "Never seen")) == (False, None)


def test_evicted_keys_come_back_from_the_store_not_the_network():
    client = StubClient({("enwiki", t): f"Q{i}" for i, t in enumerate("ABC")})
    resolver, _ = make_resolver(client, cache_size=1)

    resolver.resolve_block([("enwiki", "A"), ("enwiki", "B"), ("enwiki", "C")])
    requests_after_first_pass = len(client.calls)
    assert resolver.resolve_block([("enwiki", "A")]) == ["Q0"]

    assert len(client.calls) == requests_after_first_pass
    assert resolver.stats.store_hits == 1
    assert resolver.cache.evictions >= 2


# --- negative caching --------------------------------------------------------


def test_pages_without_an_item_are_cached_negatively():
    client = StubClient({})
    resolver, _ = make_resolver(client)

    assert resolver.resolve_block([("dewiki", "Kein Item")]) == [None]
    assert resolver.resolve_block([("dewiki", "Kein Item")]) == [None]

    assert len(client.calls) == 1
    assert resolver.stats.negative_cache_hits == 1
    assert resolver.stats.cache_hit_rate == 0.5


def test_negative_results_survive_in_the_store():
    store = ResolutionStore(":memory:")
    store.put_many({("dewiki", "Kein Item"): None, ("enwiki", "Tokyo"): "Q1490"})
    assert store.get(("dewiki", "Kein Item")) == (True, None)
    assert store.get(("enwiki", "Tokyo")) == (True, "Q1490")
    assert store.get(("enwiki", "Absent")) == (False, None)


# --- batching ----------------------------------------------------------------


def test_lookups_are_batched_per_site_up_to_the_batch_size():
    client = StubClient()
    resolver, _ = make_resolver(client, batch_size=50)
    keys = [("enwiki", f"T{i}") for i in range(120)] + [("jawiki", "東京")]

    resolver.resolve_block(keys)

    sizes = sorted((site, len(titles)) for site, titles in client.calls)
    assert sizes == [("enwiki", 20), ("enwiki", 50), ("enwiki", 50), ("jawiki", 1)]


def test_repeats_within_a_block_cost_one_lookup():
    client = StubClient({("enwiki", "Tokyo"): "Q1490"})
    resolver, _ = make_resolver(client)

    result = resolver.resolve_block([("enwiki", "Tokyo")] * 3)

    assert result == ["Q1490"] * 3
    assert client.calls == [("enwiki", ("Tokyo",))]
    assert resolver.stats.coalesced == 2


# --- backoff -----------------------------------------------------------------


def test_backoff_doubles_on_rate_limits_then_succeeds():
    client = StubClient({("enwiki", "A"): "Q1"}, [RateLimited(None), RateLimited(None)])
    resolver, sleeps = make_resolver(client)

    assert resolver.resolve_block([("enwiki", "A")]) == ["Q1"]
    assert sleeps == [1.0, 2.0]
    assert resolver.stats.rate_limited == 2
    assert resolver.stats.retries == 2


def test_backoff_honours_retry_after_but_caps_it():
    client = StubClient({}, [RateLimited(5.0), RateLimited(600.0)])
    resolver, sleeps = make_resolver(client, backoff_max_seconds=8.0)
    resolver.resolve_block([("enwiki", "A")])
    assert sleeps == [5.0, 8.0]


def test_exhausted_retries_are_counted_and_not_cached():
    client = StubClient({}, [TransientError("boom")] * 4)
    resolver, sleeps = make_resolver(client, max_retries=3)

    assert resolver.resolve_block([("enwiki", "A")]) == [None]
    assert sleeps == [1.0, 2.0, 4.0]
    assert resolver.stats.failed_titles == 1

    # Not cached as "no item": the next block asks again.
    resolver.resolve_block([("enwiki", "A")])
    assert len(client.calls) == 5


def test_api_error_is_bisected_to_the_offending_title():
    class RejectsOne(StubClient):
        def lookup(self, site, titles):
            self.calls.append((site, tuple(titles)))
            if "Bad|Title" in titles:
                raise ApiError("param-illegal")
            return dict.fromkeys(titles, "Q1")

    client = RejectsOne()
    resolver, sleeps = make_resolver(client)
    keys = [("enwiki", t) for t in ["A", "B", "Bad|Title", "C"]]

    assert resolver.resolve_block(keys) == ["Q1", "Q1", None, "Q1"]
    assert resolver.stats.api_errors == 1
    assert sleeps == []  # API errors are not retried unchanged


# --- offline -----------------------------------------------------------------


def test_offline_resolver_uses_only_the_snapshot():
    store = ResolutionStore(":memory:")
    store.put_many({("enwiki", "Tokyo"): "Q1490"})
    resolver = Resolver(make_config(), store, client=None)

    assert resolver.resolve_block([("enwiki", "Tokyo"), ("enwiki", "Unseen")]) == [
        "Q1490",
        None,
    ]
    assert resolver.stats.offline_unresolved == 1
    assert store.get(("enwiki", "Unseen")) == (False, None)


# --- HTTP client (mock transport, no sockets) --------------------------------


def http_client(handler):
    transport = httpx.MockTransport(handler)
    return HttpWikidataClient(make_config(), httpx.Client(transport=transport))


def test_http_client_posts_a_batched_request_and_parses_it():
    seen = {}

    def handler(request):
        seen.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json=REAL_RESPONSE)

    result = http_client(handler).lookup("enwiki", ["Tokyo", "UK"])

    assert result == {"Tokyo": "Q1490", "UK": None}
    assert seen["titles"] == "Tokyo|UK"
    assert seen["sites"] == "enwiki"
    assert seen["maxlag"] == "5"


def test_http_client_maps_429_to_rate_limited_with_retry_after():
    client = http_client(lambda r: httpx.Response(429, headers={"Retry-After": "7"}))
    with pytest.raises(RateLimited) as excinfo:
        client.lookup("enwiki", ["A"])
    assert excinfo.value.retry_after == 7.0


def test_http_client_maps_maxlag_refusal_to_rate_limited():
    body = {"error": {"code": "maxlag", "info": "Waiting for replicas"}}
    client = http_client(
        lambda r: httpx.Response(200, json=body, headers={"Retry-After": "5"})
    )
    with pytest.raises(RateLimited):
        client.lookup("enwiki", ["A"])


def test_http_client_maps_5xx_and_transport_errors_to_transient():
    with pytest.raises(TransientError):
        http_client(lambda r: httpx.Response(502)).lookup("enwiki", ["A"])

    def disconnect(request):
        raise httpx.ConnectError("reset")

    with pytest.raises(TransientError):
        http_client(disconnect).lookup("enwiki", ["A"])


def test_http_client_maps_other_api_errors_to_api_error():
    body = {"error": {"code": "param-illegal", "info": "bad title"}}
    with pytest.raises(ApiError):
        http_client(lambda r: httpx.Response(200, json=body)).lookup("enwiki", ["A"])


# --- guards ------------------------------------------------------------------


def test_the_suite_cannot_reach_the_network():
    with pytest.raises(RuntimeError, match="must not touch the network"):
        socket.create_connection(("www.wikidata.org", 443))


def test_percentile_is_none_without_data():
    assert percentile([], 0.5) is None
    assert percentile([3.0, 1.0, 2.0], 0.5) == 2.0
    assert json.dumps(percentile([1.0], 0.95)) == "1.0"
