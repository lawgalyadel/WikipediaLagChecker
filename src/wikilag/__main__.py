"""Command line entry point.

python -m wikilag archive --max-events 500
python -m wikilag stats
"""

from __future__ import annotations

import argparse
import sys

import structlog

from wikilag.archiver import run_archiver
from wikilag.config import load_config
from wikilag.logging_setup import configure
from wikilag.replay import describe

log = structlog.get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wikilag")
    parser.add_argument("--config", default=None, help="path to a TOML config file")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    archive = sub.add_parser("archive", help="stream and archive raw events")
    archive.add_argument("--max-events", type=int, default=None)

    sub.add_parser("stats", help="describe what is in the archive")

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

    return 0


if __name__ == "__main__":
    sys.exit(main())
