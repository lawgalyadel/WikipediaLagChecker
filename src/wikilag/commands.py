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

from wikilag import failures, report
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
from wikilag.replay import describe, replay_partitions
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


def run_stats(config: Config, pattern: str | None, run_id: str) -> None:
    paths = select_partitions(config, pattern)
    stats = describe(config.archive.directory, paths)

    gap_minutes = 0
    for gap in stats.gaps:
        gap_minutes += gap[2]

    summary = {}
    summary["run_id"] = run_id
    summary["window"] = _time_window(paths)
    for key, value in asdict(stats).items():
        summary[key] = value
    summary["undecodable_rate"] = round(stats.undecodable_rate, 6)
    summary["gap_minutes"] = gap_minutes

    path = write_result(config, "archive", summary)
    log.info(
        "archive.stats",
        result=str(path),
        partitions=stats.partitions,
        events=stats.events,
        undecodable=stats.undecodable,
        damaged_members=stats.damaged_members,
        gaps=len(stats.gaps),
    )


def run_resolve(config: Config, pattern: str | None, offline: bool, run_id: str) -> None:
    paths = select_partitions(config, pattern)
    store = ResolutionStore(config.wikidata.store_path)

    if offline:
        client = None
    else:
        client = HttpWikidataClient(config.wikidata)

    resolver = Resolver(config.wikidata, store, client)
    counts = EventCounts()

    log.info("resolve.start", partitions=len(paths), offline=offline, store=len(store))
    started = time.perf_counter()

    edits_done = 0
    with_item = 0
    for _edit, qid in resolved_edits(paths, config, resolver, counts):
        edits_done += 1
        if qid is not None:
            with_item += 1

        if edits_done % config.wikidata.block_events == 0:
            log.info(
                "resolve.progress",
                edits=edits_done,
                cache_hit_rate=round(resolver.stats.cache_hit_rate, 4),
                network_requests=resolver.stats.network_requests,
            )

    elapsed = time.perf_counter() - started

    summary = {
        "run_id": run_id,
        "offline": offline,
        "partitions": _partition_names(paths),
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

    records_path = config.results.records_path
    records_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    with (
        OfflineResolver(config) as resolver,
        gzip.open(records_path, "wt", encoding="utf-8", newline="") as handle,
    ):
        writer = csv.DictWriter(handle, fieldnames=RECORD_FIELDS)
        writer.writeheader()

        # Called by the join every time it emits a record.
        def on_record(record: PropagationRecord) -> None:
            aggregator.add_record(record)
            row = asdict(record)
            row["lag_seconds"] = record.lag_seconds
            writer.writerow(row)

        join = PropagationJoin(config.join, on_record, aggregator.add_window)
        for edit, qid in resolved_edits(paths, config, resolver, counts):
            join.push(edit, qid)
        join.finish()

        resolver_summary = resolver.stats.summary(resolver.cache.evictions)

    elapsed = time.perf_counter() - started

    stats = join.stats
    closed = stats.closed_with_human_edit

    reached_rank_rates = {}
    for rank, count in stats.reached_rank.items():
        reached_rank_rates[str(rank)] = _rate(count, closed)

    # Windows that never got a second edition.
    reached_second = stats.reached_rank.get(2, 0)
    never_propagated = _rate(closed - reached_second, closed)

    rates = {
        "reached_rank": reached_rank_rates,
        "never_propagated": never_propagated,
        "late_followup_items": stats.late_followup_rate,
        "bot_share_of_cross_edition_edits": stats.bot_share_of_cross_edition_edits,
        "edits_without_item": _rate(stats.without_item, stats.edits_in),
    }

    summary = {
        "run_id": run_id,
        "partitions": _partition_names(paths),
        "window": _time_window(paths),
        "watermark_seconds": config.join.watermark_seconds,
        "warmup_seconds": config.join.warmup_seconds,
        "reorder_seconds": config.join.reorder_seconds,
        "events": asdict(counts),
        "join": asdict(stats),
        "rates": rates,
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
        **rates,
    )


def run_pairs_sample(
    config: Config, pattern: str | None, force: bool, run_id: str
) -> None:
    labels = config.baseline.labels_path

    # Don't overwrite a sheet someone may already have labelled.
    if labels.exists() and not force:
        raise SystemExit(
            f"{labels} exists and may hold hand labels; pass --force to replace it"
        )

    paths = select_partitions(config, pattern)
    with OfflineResolver(config) as resolver:
        resolved = resolved_edits(paths, config, resolver, EventCounts())
        pair_sets = build_pairs(resolved)
        unresolved = resolver.stats.offline_unresolved

    rows, key, allocation = sample_for_labelling(
        pair_sets, config.baseline.sample_size, config.baseline.sample_seed
    )
    write_labelling_files(labels, rows, key)

    stratum_sizes = {}
    for name, pairs in pair_sets.strata().items():
        stratum_sizes[name] = len(pairs)

    summary = {
        "run_id": run_id,
        "partitions": _partition_names(paths),
        "window": _time_window(paths),
        "pages": pair_sets.pages,
        "pages_with_item": pair_sets.pages_with_item,
        "unresolved_keys": unresolved,
        "naive_pairs": len(pair_sets.naive),
        "wikidata_pairs": len(pair_sets.wikidata),
        "stratum_sizes": stratum_sizes,
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

    summary = {}
    summary["run_id"] = run_id
    summary["window"] = pairs_result["window"]
    summary["naive_pairs"] = pairs_result["naive_pairs"]
    summary["wikidata_pairs"] = pairs_result["wikidata_pairs"]
    for key, value in result.items():
        summary[key] = value

    path = write_result(config, "baseline", summary)
    log.info("pairs.evaluated", result=str(path), labelled=result["labelled"])


def run_bench(config: Config, pattern: str | None, label: str, run_id: str) -> None:
    paths = select_partitions(config, pattern)
    log.info("bench.start", partitions=len(paths), workers=config.bench.workers)

    result = benchmark(config, paths)

    document = {"run_id": run_id}
    for key, value in result.items():
        document[key] = value
    path = write_result(config, f"bench_{label}", document)

    for row in result["rows"]:
        # The digests are long and not useful in the log.
        fields = {}
        for key, value in row.items():
            if key != "digests":
                fields[key] = value
        log.info("bench.row", **fields)

    log.info(
        "bench.done",
        result=str(path),
        deterministic_across_workers=result["deterministic_across_workers"],
    )


def run_profile(config: Config, pattern: str | None, label: str, run_id: str) -> None:
    paths = select_partitions(config, pattern)
    result, profile_text = profile(config, paths)

    document = {"run_id": run_id}
    for key, value in result.items():
        document[key] = value
    path = write_result(config, f"profile_{label}", document)

    text_path = config.results.directory / f"profile_{label}.txt"
    text_path.write_text(profile_text, encoding="utf-8")

    for row in result["top"][:8]:
        log.info("profile.top", **row)
    log.info("profile.done", result=str(path), elapsed_seconds=result["elapsed_seconds"])


def run_failures_sample(
    config: Config, pattern: str | None, force: bool, run_id: str
) -> None:
    sheet = config.failures.sheet_path

    # Don't overwrite a sheet someone may already have reviewed.
    if sheet.exists() and not force:
        raise SystemExit(
            f"{sheet} exists and may hold reviews; pass --force to replace it"
        )

    records_path = config.results.records_path
    if not records_path.exists():
        raise SystemExit(f"{records_path} not found; run `wikilag join` first")

    all_records = failures.read_records(records_path)
    sampled = failures.sample_records(
        all_records,
        config.failures.candidates,
        config.failures.sample_seed,
    )

    # Both edits of every sampled record need their evidence looked up.
    wanted = set()
    for record in sampled:
        wanted.add(failures.edit_key(record, "leader"))
        wanted.add(failures.edit_key(record, "follower"))

    events = replay_partitions(select_partitions(config, pattern))
    evidence = failures.evidence_from_events(events, wanted)

    rows = failures.build_sheet(sampled, evidence, config)
    failures.write_sheet(sheet, rows)

    suggested: dict[str, int] = {}
    for row in rows:
        category = row["suggested_category"]
        if category in suggested:
            suggested[category] += 1
        else:
            suggested[category] = 1

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

    document = {"run_id": run_id}
    for key, value in result.items():
        document[key] = value
    path = write_result(config, "failures", document)

    log.info(
        "failures.summarised",
        result=str(path),
        reviewed=result["reviewed"],
        confirmed_wrong=result["confirmed_wrong"],
    )


def run_report(config: Config, run_id: str) -> None:
    results = report.load_results(config.results.directory)
    rendered = report.render(results)
    changed = report.update_readme(config.results.readme_path, rendered)
    log.info("report.done", readme=str(config.results.readme_path), changed=changed)


def _read_result(config: Config, name: str) -> dict:
    path = config.results.directory / f"{name}.json"
    if not path.exists():
        raise SystemExit(f"{path} not found; run the command that produces it first")
    text = path.read_text(encoding="utf-8")
    return json.loads(text)


def _rate(numerator: int, denominator: int) -> float | None:
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def _partition_names(paths: list[Path]) -> list[str]:
    names = []
    for path in paths:
        names.append(path.name)
    return names


def _time_window(paths: list[Path]) -> dict:
    if len(paths) == 0:
        first_partition = None
        last_partition = None
    else:
        first_partition = paths[0].name
        last_partition = paths[-1].name

    return {
        "first_partition": first_partition,
        "last_partition": last_partition,
        "partitions": len(paths),
    }
