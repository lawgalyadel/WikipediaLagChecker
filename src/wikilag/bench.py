"""Replay throughput, memory and scaling, plus the profiler used before
optimising anything.

Parallelism is per partition: decoding gzip, parsing JSON and the
stateless filters run in worker processes, and partitions come back in
order. Deduplication, resolution and the join are ordered and stateful, so
they stay in the main process. That split is where scaling is expected to
stop being linear, so the parse stage is timed on its own as well: if the
full pipeline flattens while parse keeps scaling, the serial stage is the
ceiling (Amdahl), not the workers.

Every worker count must produce the same output. A digest of the join's
records and counters is compared across runs, so a speed-up that changes
the answer is caught rather than reported.
"""

from __future__ import annotations

import cProfile
import hashlib
import io
import multiprocessing
import pstats
import statistics
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

import psutil

from wikilag.config import Config
from wikilag.events import Edit, EventCounts, deduplicated, filtered
from wikilag.pipeline import resolve_in_blocks
from wikilag.propagation import PropagationJoin, PropagationRecord
from wikilag.replay import replay_partitions
from wikilag.resolver import ResolutionStore, Resolver


def parse_partition(task: tuple[str, Config]) -> tuple[list[Edit], EventCounts]:
    """Worker body. Top-level so spawn-based pools (Windows) can import it."""
    path, config = task
    counts = EventCounts()
    return list(filtered(replay_partitions([Path(path)]), config, counts)), counts


def _merge(total: EventCounts, part: EventCounts) -> None:
    total.seen += part.seen
    total.malformed += part.malformed
    for reason, count in part.filtered.items():
        total.filtered[reason] = total.filtered.get(reason, 0) + count


class PeakMemory:
    """Samples resident memory of this process and its children.

    Children are enumerated once on entry. Enumerating per sample walks the
    whole process table on Windows, and the first profile showed that
    costing a fifth of runtime — the harness distorting what it measures.
    Pool workers live for the whole run, so the list stays accurate.
    """

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._process = psutil.Process()
        self._children: list[psutil.Process] = []
        self.peak_bytes = 0

    def _sample(self) -> int:
        total = self._process.memory_info().rss
        for child in self._children:
            try:
                total += child.memory_info().rss
            except psutil.Error:
                continue
        return total

    def _run(self) -> None:
        while not self._stop.is_set():
            self.peak_bytes = max(self.peak_bytes, self._sample())
            self._stop.wait(self._interval)

    def __enter__(self) -> PeakMemory:
        self._children = self._process.children(recursive=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._thread.join()
        self.peak_bytes = max(self.peak_bytes, self._sample())


def _parsed(
    config: Config, paths: list[Path], workers: int, pool
) -> Iterator[tuple[list[Edit], EventCounts]]:
    tasks = [(str(path), config) for path in paths]
    if workers == 1:
        return map(parse_partition, tasks)
    return pool.imap(parse_partition, tasks)


class _NoMemory:
    peak_bytes = 0

    def __enter__(self) -> _NoMemory:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def run_once(
    config: Config,
    paths: list[Path],
    workers: int,
    stage: str,
    pool,
    measure_memory: bool = True,
) -> dict:
    """One timed replay. `stage` is "parse" (workers only) or "full"."""
    counts = EventCounts()
    digest = hashlib.sha256()
    records = 0

    def candidates() -> Iterator[Edit]:
        for part_edits, part_counts in _parsed(config, paths, workers, pool):
            _merge(counts, part_counts)
            yield from part_edits

    sampler = (
        PeakMemory(config.bench.rss_sample_interval_seconds)
        if measure_memory
        else _NoMemory()
    )
    with sampler as memory:
        started = time.perf_counter()
        if stage == "parse":
            for _ in candidates():
                pass
        else:
            store = ResolutionStore(config.wikidata.store_path)
            resolver = Resolver(config.wikidata, store, client=None)

            def on_record(record: PropagationRecord) -> None:
                nonlocal records
                records += 1
                digest.update(repr(asdict(record)).encode())

            join = PropagationJoin(config.join, on_record)
            stream = deduplicated(candidates(), config, counts)
            for edit, qid in resolve_in_blocks(stream, config, resolver):
                join.push(edit, qid)
            join.finish()
            digest.update(repr(asdict(join.stats)).encode())
            store.close()
        elapsed = time.perf_counter() - started

    return {
        "elapsed_seconds": elapsed,
        "events": counts.seen,
        "peak_rss_bytes": memory.peak_bytes,
        "records": records,
        "digest": digest.hexdigest()[:16] if stage == "full" else None,
    }


def benchmark(config: Config, paths: list[Path]) -> dict:
    context = multiprocessing.get_context("spawn")
    rows = []
    for stage in ("parse", "full"):
        for workers in config.bench.workers:
            pool = context.Pool(workers) if workers > 1 else None
            try:
                runs = [
                    run_once(config, paths, workers, stage, pool)
                    for _ in range(config.bench.repeats)
                ]
            finally:
                if pool is not None:
                    pool.close()
                    pool.join()
            elapsed = statistics.median(run["elapsed_seconds"] for run in runs)
            events = runs[0]["events"]
            rows.append(
                {
                    "stage": stage,
                    "workers": workers,
                    "events": events,
                    "median_seconds": round(elapsed, 3),
                    "events_per_second": round(events / elapsed),
                    "peak_rss_mb": round(
                        max(r["peak_rss_bytes"] for r in runs) / 2**20, 1
                    ),
                    "digests": sorted({r["digest"] for r in runs if r["digest"]}),
                    "records": runs[0]["records"],
                }
            )

    for stage in ("parse", "full"):
        stage_rows = [row for row in rows if row["stage"] == stage]
        base = stage_rows[0]["events_per_second"]
        for row in stage_rows:
            row["speedup"] = round(row["events_per_second"] / base, 2)
            row["efficiency"] = round(row["speedup"] / row["workers"], 2)

    full_digests = {d for row in rows if row["stage"] == "full" for d in row["digests"]}
    return {
        "partitions": [p.name for p in paths],
        "cpu_count": psutil.cpu_count(logical=True),
        "physical_cores": psutil.cpu_count(logical=False),
        "repeats": config.bench.repeats,
        "rows": rows,
        "deterministic_across_workers": len(full_digests) == 1,
    }


def profile(config: Config, paths: list[Path]) -> tuple[dict, str]:
    """cProfile of one single-worker full replay: top functions by own time.

    The memory sampler is off: this profiles the pipeline, not the harness.
    """
    profiler = cProfile.Profile()
    profiler.enable()
    result = run_once(
        config, paths, workers=1, stage="full", pool=None, measure_memory=False
    )
    profiler.disable()

    buffer = io.StringIO()
    # strip_dirs: the report is committed, local install paths are not.
    stats = pstats.Stats(profiler, stream=buffer).strip_dirs().sort_stats("tottime")
    stats.print_stats(config.bench.profile_top_functions)

    total = sum(entry[2] for entry in stats.stats.values())  # type: ignore[attr-defined]
    top = sorted(
        stats.stats.items(),  # type: ignore[attr-defined]
        key=lambda item: item[1][2],
        reverse=True,
    )[: config.bench.profile_top_functions]
    rows = [
        {
            "function": f"{Path(file).name}:{line}({name})",
            "calls": calls,
            "own_seconds": round(own, 3),
            "own_share": round(own / total, 4) if total else None,
            "cumulative_seconds": round(cumulative, 3),
        }
        for (file, line, name), (_, calls, own, cumulative, _) in top
    ]
    return {
        "elapsed_seconds": round(result["elapsed_seconds"], 3),
        "top": rows,
    }, buffer.getvalue()
