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

    matching = config.archive.directory.glob(pattern)
    return sorted(matching)


def resolved_edits(
    paths: Iterable[Path], config: Config, resolver: Resolver, counts: EventCounts
) -> Iterator[tuple[Edit, str | None]]:
    """Edits paired with their Wikidata item, resolved a block at a time."""
    raw_events = replay_partitions(paths)
    article_edits = edits(raw_events, config, counts)
    return resolve_in_blocks(article_edits, config, resolver)


def resolve_in_blocks(
    stream: Iterable[Edit], config: Config, resolver: Resolver
) -> Iterator[tuple[Edit, str | None]]:
    block: list[Edit] = []

    for edit in stream:
        block.append(edit)

        if len(block) >= config.wikidata.block_events:
            for pair in _resolve(block, resolver):
                yield pair
            block = []

    # Whatever is left over at the end is a smaller final block.
    if len(block) > 0:
        for pair in _resolve(block, resolver):
            yield pair


def _resolve(block: list[Edit], resolver: Resolver) -> list[tuple[Edit, str | None]]:
    keys = []
    for edit in block:
        keys.append(edit.key)

    qids = resolver.resolve_block(keys)
    if len(qids) != len(block):
        raise ValueError("resolver returned a different number of results than keys")

    pairs = []
    for index in range(len(block)):
        pairs.append((block[index], qids[index]))
    return pairs


def counts_summary(counts: EventCounts) -> dict:
    return asdict(counts)


def write_result(config: Config, name: str, payload: dict) -> Path:
    """Persist a measured result. The README tables are rendered from these."""
    directory = config.results.directory
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"

    document = {}
    document["generated_at"] = datetime.now(UTC).isoformat()
    for key, value in payload.items():
        document[key] = value

    text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False)
    path.write_text(text)
    return path
