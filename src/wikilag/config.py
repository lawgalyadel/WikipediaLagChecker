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
class JoinConfig:
    watermark_seconds: int


@dataclass(frozen=True)
class Config:
    stream: StreamConfig
    archive: ArchiveConfig
    filters: FilterConfig
    join: JoinConfig
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
        join=JoinConfig(watermark_seconds=raw["join"]["watermark_seconds"]),
        raw=raw,
    )
