"""Configuration loading.

Every tunable in this project lives in a TOML file. No thresholds, paths,
wiki lists or watermarks are hardcoded anywhere else in the package.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("config/default.toml")


@dataclass(frozen=True)
class StreamConfig:
    url: str
    reconnect_initial_seconds: float
    reconnect_max_seconds: float
    read_timeout_seconds: float


@dataclass(frozen=True)
class ArchiveConfig:
    directory: Path
    partition_format: str
    offset_file: Path
    flush_every_events: int


@dataclass(frozen=True)
class FilterConfig:
    wikis: list[str]
    namespaces: list[int]
    include_bots: bool


@dataclass(frozen=True)
class EventsConfig:
    change_types: list[str]
    dedupe_window_events: int


@dataclass(frozen=True)
class WikidataConfig:
    api_url: str
    user_agent: str
    batch_size: int
    cache_size: int
    store_path: Path
    store_query_chunk: int
    request_timeout_seconds: float
    maxlag_seconds: int
    max_retries: int
    backoff_initial_seconds: float
    backoff_max_seconds: float
    block_events: int


@dataclass(frozen=True)
class ResultsConfig:
    directory: Path


@dataclass(frozen=True)
class JoinConfig:
    watermark_seconds: int
    reorder_seconds: int
    warmup_seconds: int
    emit_ranks: list[int]
    late_horizon_seconds: int
    min_pair_samples: int


@dataclass(frozen=True)
class BenchConfig:
    workers: list[int]
    repeats: int
    rss_sample_interval_seconds: float
    profile_top_functions: int


@dataclass(frozen=True)
class BaselineConfig:
    sample_size: int
    sample_seed: int
    labels_path: Path


@dataclass(frozen=True)
class Config:
    stream: StreamConfig
    archive: ArchiveConfig
    filters: FilterConfig
    events: EventsConfig
    wikidata: WikidataConfig
    results: ResultsConfig
    join: JoinConfig
    baseline: BaselineConfig
    bench: BenchConfig
    raw: dict = field(default_factory=dict, repr=False)


def load_config(path: str | Path | None = None) -> Config:
    """Load config from TOML.

    Path precedence: explicit argument, then $WIKILAG_CONFIG, then the
    repo default. Kept in that order so docker-compose can point at a
    different file without touching the image.
    """
    resolved = Path(path or os.environ.get("WIKILAG_CONFIG") or DEFAULT_CONFIG_PATH)
    with resolved.open("rb") as handle:
        raw = tomllib.load(handle)

    wikidata = raw["wikidata"]
    join = raw["join"]
    return Config(
        stream=StreamConfig(
            url=raw["stream"]["url"],
            reconnect_initial_seconds=raw["stream"]["reconnect_initial_seconds"],
            reconnect_max_seconds=raw["stream"]["reconnect_max_seconds"],
            read_timeout_seconds=raw["stream"]["read_timeout_seconds"],
        ),
        archive=ArchiveConfig(
            directory=Path(raw["archive"]["directory"]),
            partition_format=raw["archive"]["partition_format"],
            offset_file=Path(raw["archive"]["offset_file"]),
            flush_every_events=raw["archive"]["flush_every_events"],
        ),
        filters=FilterConfig(
            wikis=list(raw["filters"]["wikis"]),
            namespaces=list(raw["filters"]["namespaces"]),
            include_bots=raw["filters"]["include_bots"],
        ),
        events=EventsConfig(
            change_types=list(raw["events"]["change_types"]),
            dedupe_window_events=raw["events"]["dedupe_window_events"],
        ),
        wikidata=WikidataConfig(
            api_url=wikidata["api_url"],
            user_agent=wikidata["user_agent"],
            batch_size=wikidata["batch_size"],
            cache_size=wikidata["cache_size"],
            store_path=Path(wikidata["store_path"]),
            store_query_chunk=wikidata["store_query_chunk"],
            request_timeout_seconds=wikidata["request_timeout_seconds"],
            maxlag_seconds=wikidata["maxlag_seconds"],
            max_retries=wikidata["max_retries"],
            backoff_initial_seconds=wikidata["backoff_initial_seconds"],
            backoff_max_seconds=wikidata["backoff_max_seconds"],
            block_events=wikidata["block_events"],
        ),
        results=ResultsConfig(directory=Path(raw["results"]["directory"])),
        join=JoinConfig(
            watermark_seconds=join["watermark_seconds"],
            reorder_seconds=join["reorder_seconds"],
            warmup_seconds=join["warmup_seconds"],
            emit_ranks=sorted(join["emit_ranks"]),
            late_horizon_seconds=join["late_horizon_seconds"],
            min_pair_samples=join["min_pair_samples"],
        ),
        baseline=BaselineConfig(
            sample_size=raw["baseline"]["sample_size"],
            sample_seed=raw["baseline"]["sample_seed"],
            labels_path=Path(raw["baseline"]["labels_path"]),
        ),
        bench=BenchConfig(
            workers=list(raw["bench"]["workers"]),
            repeats=raw["bench"]["repeats"],
            rss_sample_interval_seconds=raw["bench"]["rss_sample_interval_seconds"],
            profile_top_functions=raw["bench"]["profile_top_functions"],
        ),
        raw=raw,
    )
