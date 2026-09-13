"""Command line entry point.

python -m wikilag archive --max-events 500
python -m wikilag stats
python -m wikilag resolve --partitions "2026-09-13-15.jsonl.gz"
python -m wikilag resolve --offline
"""

from __future__ import annotations

import argparse
import sys
import time

import structlog

from wikilag.archiver import run_archiver
from wikilag.config import Config, load_config
from wikilag.events import EventCounts
from wikilag.logging_setup import configure
from wikilag.pipeline import (
    counts_summary,
    resolved_edits,
    select_partitions,
    write_result,
)
from wikilag.replay import describe
from wikilag.resolver import HttpWikidataClient, ResolutionStore, Resolver

log = structlog.get_logger(__name__)


def run_resolve(config: Config, pattern: str | None, offline: bool, run_id: str) -> None:
    paths = select_partitions(config, pattern)
    store = ResolutionStore(config.wikidata.store_path)
    client = None if offline else HttpWikidataClient(config.wikidata)
    resolver = Resolver(config.wikidata, store, client)
    counts = EventCounts()

    log.info("resolve.start", partitions=len(paths), offline=offline, store=len(store))
    started = time.perf_counter()
    with_item = 0
    for index, (_, qid) in enumerate(resolved_edits(paths, config, resolver, counts), 1):
        with_item += qid is not None
        if index % config.wikidata.block_events == 0:
            log.info(
                "resolve.progress",
                edits=index,
                cache_hit_rate=round(resolver.stats.cache_hit_rate, 4),
                network_requests=resolver.stats.network_requests,
            )
    elapsed = time.perf_counter() - started

    summary = {
        "run_id": run_id,
        "offline": offline,
        "partitions": [p.name for p in paths],
        "events": counts_summary(counts),
        "edits_with_item": with_item,
        "edits_with_item_rate": round(with_item / counts.kept, 4)
        if counts.kept
        else None,
        "resolver": resolver.stats.summary(resolver.cache.evictions),
        "cache_size": config.wikidata.cache_size,
        "store_entries": len(store),
        "elapsed_seconds": round(elapsed, 2),
    }
    path = write_result(config, "resolve", summary)
    log.info("resolve.done", result=str(path), **summary["resolver"])

    if client is not None:
        client.close()
    store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wikilag")
    parser.add_argument("--config", default=None, help="path to a TOML config file")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    archive = sub.add_parser("archive", help="stream and archive raw events")
    archive.add_argument("--max-events", type=int, default=None)

    sub.add_parser("stats", help="describe what is in the archive")

    resolve = sub.add_parser("resolve", help="map archived edits to Wikidata items")
    resolve.add_argument("--partitions", default=None, help="glob within the archive")
    resolve.add_argument(
        "--offline", action="store_true", help="use only the resolution snapshot"
    )

    args = parser.parse_args(argv)
    run_id = configure(args.log_level)
    config = load_config(args.config)

    if args.command == "archive":
        log.info("archive.start", run_id=run_id, url=config.stream.url)
        archived = run_archiver(config, max_events=args.max_events)
        log.info("archive.stop", archived=archived)
    elif args.command == "stats":
        stats = describe(config.archive.directory)
        log.info(
            "archive.stats",
            partitions=stats.partitions,
            events=stats.events,
            undecodable=stats.undecodable,
            undecodable_rate=round(stats.undecodable_rate, 6),
        )
    elif args.command == "resolve":
        run_resolve(config, args.partitions, args.offline, run_id)

    return 0


if __name__ == "__main__":
    sys.exit(main())
