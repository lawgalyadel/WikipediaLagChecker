"""Implementations behind each CLI subcommand.

Each command reads the archive, writes one JSON document to the results
directory, and logs a summary. Analysis commands resolve offline from the
snapshot, so they are deterministic and never touch the network; only
`resolve` fetches.
"""

from __future__ import annotations

import csv
import gzip
import json
import time
from dataclasses import asdict
from pathlib import Path

import structlog

from wikilag import failures
from wikilag.analysis import LagAggregator
from wikilag.bench import benchmark, profile
from wikilag.config import Config
from wikilag.events import EventCounts
from wikilag.pairs import (
    build_pairs,
    evaluate,
    key_path,
    sample_for_labelling,
    write_labelling_files,
)
from wikilag.pipeline import resolved_edits, select_partitions, write_result
from wikilag.propagation import PropagationJoin, PropagationRecord
from wikilag.replay import replay_partitions
from wikilag.resolver import HttpWikidataClient, ResolutionStore, Resolver

log = structlog.get_logger(__name__)

RECORD_FIELDS = [
    "qid",
    "rank",
    "leader_wiki",
    "leader_title",
    "leader_timestamp",
    "follower_wiki",
    "follower_title",
    "follower_timestamp",
    "follower_user",
    "lag_seconds",
]


class OfflineResolver:
    """Context manager for the snapshot-only resolver used by analyses."""

    def __init__(self, config: Config) -> None:
        self._config = config

    def __enter__(self) -> Resolver:
        self._store = ResolutionStore(self._config.wikidata.store_path)
        return Resolver(self._config.wikidata, self._store, client=None)

    def __exit__(self, *exc_info: object) -> None:
        self._store.close()


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
        "events": asdict(counts),
        "edits_with_item": with_item,
        "edits_with_item_rate": _rate(with_item, counts.kept),
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


def run_join(config: Config, pattern: str | None, run_id: str) -> None:
    paths = select_partitions(config, pattern)
    counts = EventCounts()
    aggregator = LagAggregator()
    records_path = config.results.directory / "propagation_records.csv.gz"
    records_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    with (
        OfflineResolver(config) as resolver,
        gzip.open(records_path, "wt", encoding="utf-8", newline="") as handle,
    ):
        writer = csv.DictWriter(handle, fieldnames=RECORD_FIELDS)
        writer.writeheader()

        def on_record(record: PropagationRecord) -> None:
            aggregator.add_record(record)
            writer.writerow({**asdict(record), "lag_seconds": record.lag_seconds})

        join = PropagationJoin(config.join, on_record, aggregator.add_window)
        for edit, qid in resolved_edits(paths, config, resolver, counts):
            join.push(edit, qid)
        join.finish()
        resolver_summary = resolver.stats.summary(resolver.cache.evictions)
    elapsed = time.perf_counter() - started

    stats = join.stats
    closed = stats.closed_with_human_edit
    summary = {
        "run_id": run_id,
        "partitions": [p.name for p in paths],
        "window": _time_window(paths),
        "watermark_seconds": config.join.watermark_seconds,
        "warmup_seconds": config.join.warmup_seconds,
        "reorder_seconds": config.join.reorder_seconds,
        "events": asdict(counts),
        "join": asdict(stats),
        "rates": {
            "reached_rank": {
                str(rank): _rate(count, closed)
                for rank, count in stats.reached_rank.items()
            },
            "never_propagated": _rate(closed - stats.reached_rank.get(2, 0), closed),
            "late_followup_items": stats.late_followup_rate,
            "bot_share_of_cross_edition_edits": stats.bot_share_of_cross_edition_edits,
            "edits_without_item": _rate(stats.without_item, stats.edits_in),
        },
        "lags": aggregator.summary(config.join.min_pair_samples),
        "resolver": resolver_summary,
        "records_file": str(records_path),
        "elapsed_seconds": round(elapsed, 2),
    }
    path = write_result(config, "propagation", summary)
    log.info(
        "join.done",
        result=str(path),
        records=aggregator.records,
        closed_windows=closed,
        **summary["rates"],
    )


def run_pairs_sample(
    config: Config, pattern: str | None, force: bool, run_id: str
) -> None:
    labels = config.baseline.labels_path
    if labels.exists() and not force:
        raise SystemExit(
            f"{labels} exists and may hold hand labels; pass --force to replace it"
        )
    paths = select_partitions(config, pattern)
    with OfflineResolver(config) as resolver:
        pair_sets = build_pairs(resolved_edits(paths, config, resolver, EventCounts()))
        unresolved = resolver.stats.offline_unresolved

    rows, key, allocation = sample_for_labelling(
        pair_sets, config.baseline.sample_size, config.baseline.sample_seed
    )
    write_labelling_files(labels, rows, key)
    strata = pair_sets.strata()
    summary = {
        "run_id": run_id,
        "partitions": [p.name for p in paths],
        "window": _time_window(paths),
        "pages": pair_sets.pages,
        "pages_with_item": pair_sets.pages_with_item,
        "unresolved_keys": unresolved,
        "naive_pairs": len(pair_sets.naive),
        "wikidata_pairs": len(pair_sets.wikidata),
        "stratum_sizes": {name: len(pairs) for name, pairs in strata.items()},
        "allocation": allocation,
        "seed": config.baseline.sample_seed,
        "labels_file": str(labels),
        "key_file": str(key_path(labels)),
    }
    path = write_result(config, "pairs", summary)
    log.info("pairs.sampled", result=str(path), labels=str(labels), **allocation)


def run_pairs_evaluate(config: Config, run_id: str) -> None:
    pairs_result = _read_result(config, "pairs")
    result = evaluate(config.baseline.labels_path, pairs_result["stratum_sizes"])
    summary = {
        "run_id": run_id,
        "window": pairs_result["window"],
        "naive_pairs": pairs_result["naive_pairs"],
        "wikidata_pairs": pairs_result["wikidata_pairs"],
        **result,
    }
    path = write_result(config, "baseline", summary)
    log.info("pairs.evaluated", result=str(path), labelled=result["labelled"])


def run_bench(config: Config, pattern: str | None, label: str, run_id: str) -> None:
    paths = select_partitions(config, pattern)
    log.info("bench.start", partitions=len(paths), workers=config.bench.workers)
    result = benchmark(config, paths)
    path = write_result(config, f"bench_{label}", {"run_id": run_id, **result})
    for row in result["rows"]:
        log.info("bench.row", **{k: v for k, v in row.items() if k != "digests"})
    log.info(
        "bench.done",
        result=str(path),
        deterministic_across_workers=result["deterministic_across_workers"],
    )


def run_profile(config: Config, pattern: str | None, label: str, run_id: str) -> None:
    paths = select_partitions(config, pattern)
    result, report = profile(config, paths)
    path = write_result(config, f"profile_{label}", {"run_id": run_id, **result})
    (config.results.directory / f"profile_{label}.txt").write_text(
        report, encoding="utf-8"
    )
    for row in result["top"][:8]:
        log.info("profile.top", **row)
    log.info("profile.done", result=str(path), elapsed_seconds=result["elapsed_seconds"])


def run_failures_sample(
    config: Config, pattern: str | None, force: bool, run_id: str
) -> None:
    sheet = config.failures.sheet_path
    if sheet.exists() and not force:
        raise SystemExit(
            f"{sheet} exists and may hold reviews; pass --force to replace it"
        )
    records_path = config.results.directory / "propagation_records.csv.gz"
    if not records_path.exists():
        raise SystemExit(f"{records_path} not found; run `wikilag join` first")

    sampled = failures.sample_records(
        failures.read_records(records_path),
        config.failures.candidates,
        config.failures.sample_seed,
    )
    wanted = {
        failures.edit_key(record, side)
        for record in sampled
        for side in ("leader", "follower")
    }
    evidence = failures.evidence_from_events(
        replay_partitions(select_partitions(config, pattern)), wanted
    )
    rows = failures.build_sheet(sampled, evidence, config)
    failures.write_sheet(sheet, rows)

    suggested: dict[str, int] = {}
    for row in rows:
        suggested[row["suggested_category"]] = (
            suggested.get(row["suggested_category"], 0) + 1
        )
    summary = {
        "run_id": run_id,
        "candidates": len(rows),
        "evidence_found": len(evidence),
        "evidence_wanted": len(wanted),
        "suggested_categories": suggested,
        "sheet": str(sheet),
    }
    path = write_result(config, "failures_sample", summary)
    log.info("failures.sampled", result=str(path), **suggested)


def run_failures_summarise(config: Config, run_id: str) -> None:
    result = failures.summarise_sheet(config.failures.sheet_path)
    path = write_result(config, "failures", {"run_id": run_id, **result})
    log.info(
        "failures.summarised",
        result=str(path),
        reviewed=result["reviewed"],
        confirmed_wrong=result["confirmed_wrong"],
    )


def _read_result(config: Config, name: str) -> dict:
    path = config.results.directory / f"{name}.json"
    if not path.exists():
        raise SystemExit(f"{path} not found; run the command that produces it first")
    return json.loads(path.read_text(encoding="utf-8"))


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _time_window(paths: list[Path]) -> dict:
    return {
        "first_partition": paths[0].name if paths else None,
        "last_partition": paths[-1].name if paths else None,
        "partitions": len(paths),
    }
