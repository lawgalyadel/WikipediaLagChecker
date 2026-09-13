"""Command line entry point.

python -m wikilag archive --max-events 500
python -m wikilag stats
python -m wikilag resolve [--partitions GLOB] [--offline]
python -m wikilag join
python -m wikilag pairs sample
python -m wikilag pairs evaluate
"""

from __future__ import annotations

import argparse
import sys

import structlog

from wikilag import commands
from wikilag.archiver import run_archiver
from wikilag.config import load_config
from wikilag.logging_setup import configure

log = structlog.get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wikilag")
    parser.add_argument("--config", default=None, help="path to a TOML config file")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    archive = sub.add_parser("archive", help="stream and archive raw events")
    archive.add_argument("--max-events", type=int, default=None)

    stats = sub.add_parser("stats", help="describe what is in the archive")
    stats.add_argument("--partitions", default=None, help="glob within the archive")

    resolve = sub.add_parser("resolve", help="map archived edits to Wikidata items")
    resolve.add_argument("--partitions", default=None, help="glob within the archive")
    resolve.add_argument(
        "--offline", action="store_true", help="use only the resolution snapshot"
    )

    join = sub.add_parser("join", help="propagation join over the archive (offline)")
    join.add_argument("--partitions", default=None, help="glob within the archive")

    pairs = sub.add_parser("pairs", help="naive vs Wikidata pair baseline")
    pairs_sub = pairs.add_subparsers(dest="pairs_command", required=True)
    sample = pairs_sub.add_parser("sample", help="write the blind labelling sheet")
    sample.add_argument("--partitions", default=None, help="glob within the archive")
    sample.add_argument("--force", action="store_true", help="replace existing labels")
    pairs_sub.add_parser("evaluate", help="precision and recall from the labels")

    failures = sub.add_parser("failures", help="failure analysis review sheet")
    failures_sub = failures.add_subparsers(dest="failures_command", required=True)
    review = failures_sub.add_parser("sample", help="write the review sheet")
    review.add_argument("--partitions", default=None, help="glob within the archive")
    review.add_argument("--force", action="store_true", help="replace existing reviews")
    failures_sub.add_parser("summarise", help="count confirmed failures by category")

    for name, help_text in (
        ("bench", "throughput and peak RSS at each worker count"),
        ("profile", "cProfile of a single-worker replay"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--partitions", default=None, help="glob within the archive")
        command.add_argument("--label", default="current", help="suffix for the result")

    sub.add_parser("report", help="render the README results from results/")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_id = configure(args.log_level)
    config = load_config(args.config)

    if args.command == "archive":
        log.info("archive.start", run_id=run_id, url=config.stream.url)
        archived = run_archiver(config, max_events=args.max_events)
        log.info("archive.stop", archived=archived)
    elif args.command == "stats":
        commands.run_stats(config, args.partitions, run_id)
    elif args.command == "resolve":
        commands.run_resolve(config, args.partitions, args.offline, run_id)
    elif args.command == "join":
        commands.run_join(config, args.partitions, run_id)
    elif args.command == "pairs" and args.pairs_command == "sample":
        commands.run_pairs_sample(config, args.partitions, args.force, run_id)
    elif args.command == "pairs" and args.pairs_command == "evaluate":
        commands.run_pairs_evaluate(config, run_id)
    elif args.command == "failures" and args.failures_command == "sample":
        commands.run_failures_sample(config, args.partitions, args.force, run_id)
    elif args.command == "failures" and args.failures_command == "summarise":
        commands.run_failures_summarise(config, run_id)
    elif args.command == "bench":
        commands.run_bench(config, args.partitions, args.label, run_id)
    elif args.command == "profile":
        commands.run_profile(config, args.partitions, args.label, run_id)
    elif args.command == "report":
        commands.run_report(config, run_id)

    return 0


if __name__ == "__main__":
    sys.exit(main())
