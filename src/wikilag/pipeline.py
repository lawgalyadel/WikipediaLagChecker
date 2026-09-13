"""Composition of the analysis stages over the archive.

archive partitions -> replay -> edits (filter, dedupe) -> resolver blocks

Kept as generators so a full archive is processed in bounded memory, and
so each stage can be measured on its own.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from wikilag.config import Config
from wikilag.events import Edit, EventCounts, edits
from wikilag.replay import partitions, replay_partitions
from wikilag.resolver import Resolver


def select_partitions(config: Config, pattern: str | None) -> list[Path]:
    """Partitions to process, in deterministic order. `pattern` is a glob."""
    if pattern is None:
        return partitions(config.archive.directory)
    return sorted(config.archive.directory.glob(pattern))


def resolved_edits(
    paths: Iterable[Path], config: Config, resolver: Resolver, counts: EventCounts
) -> Iterator[tuple[Edit, str | None]]:
    """Edits paired with their Wikidata item, resolved a block at a time."""
    block: list[Edit] = []
    for edit in edits(replay_partitions(paths), config, counts):
        block.append(edit)
        if len(block) >= config.wikidata.block_events:
            yield from zip(
                block, resolver.resolve_block([e.key for e in block]), strict=True
            )
            block = []
    if block:
        yield from zip(block, resolver.resolve_block([e.key for e in block]), strict=True)


def counts_summary(counts: EventCounts) -> dict:
    return asdict(counts)


def write_result(config: Config, name: str, payload: dict) -> Path:
    """Persist a measured result. The README tables are rendered from these."""
    directory = config.results.directory
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    document = {"generated_at": datetime.now(UTC).isoformat(), **payload}
    path.write_text(json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False))
    return path
