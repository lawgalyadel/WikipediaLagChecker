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

from WikipediaLagChecker.config import Config
from WikipediaLagChecker.events import Edit, EventCounts, deduplicated, filtered
from WikipediaLagChecker.pipeline import resolve_in_blocks
from WikipediaLagChecker.propagation import PropagationJoin, PropagationRecord
from WikipediaLagChecker.replay import replay_partitions
from WikipediaLagChecker.resolver import ResolutionStore, Resolver


def parse_partition(task: tuple[str, Config]) -> tuple[list[Edit], EventCounts]:
    """Worker body. Top-level so spawn-based pools (Windows) can import it."""
    path = task[0]
    config = task[1]

    counts = EventCounts()
    events = replay_partitions([Path(path)])
    edits = list(filtered(events, config, counts))
    return edits, counts


def _merge(total: EventCounts, part: EventCounts) -> None:
    """Add one partition's counts onto the running total."""
    total.seen += part.seen
    total.malformed += part.malformed
    for reason, count in part.filtered.items():
        if reason in total.filtered:
            total.filtered[reason] += count
        else:
            total.filtered[reason] = count


class PeakMemory:
    """Samples resident memory of this process and its children.

    Children are enumerated once on entry. Enumerating per sample walks the
    whole process table on Windows, and in the first profile that took
    about a fifth of the runtime. Pool workers live for the whole run, so
    the list stays accurate.
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
                # The child may have exited between listing and sampling.
                continue
        return total

    def _run(self) -> None:
        while not self._stop.is_set():
            current = self._sample()
            if current > self.peak_bytes:
                self.peak_bytes = current
            self._stop.wait(self._interval)

    def __enter__(self) -> PeakMemory:
        self._children = self._process.children(recursive=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._thread.join()
        # One last sample in case the peak happened right at the end.
        current = self._sample()
        if current > self.peak_bytes:
            self.peak_bytes = current


def _parsed(
    config: Config, paths: list[Path], workers: int, pool
) -> Iterator[tuple[list[Edit], EventCounts]]:
    tasks = []
    for path in paths:
        tasks.append((str(path), config))

    # One worker runs in this process; more use the pool, which keeps
    # results in partition order.
    if workers == 1:
        return map(parse_partition, tasks)
    return pool.imap(parse_partition, tasks)


class _NoMemory:
    """Stands in for PeakMemory when memory isn't being measured."""

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

    if measure_memory:
        sampler = PeakMemory(config.bench.rss_sample_interval_seconds)
    else:
        sampler = _NoMemory()

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

    short_digest = None
    if stage == "full":
        short_digest = digest.hexdigest()[:16]

    return {
        "elapsed_seconds": elapsed,
        "events": counts.seen,
        "peak_rss_bytes": memory.peak_bytes,
        "records": records,
        "digest": short_digest,
    }


def benchmark(config: Config, paths: list[Path]) -> dict:
    context = multiprocessing.get_context("spawn")
    rows = []

    for stage in ("parse", "full"):
        for workers in config.bench.workers:
            pool = None
            if workers > 1:
                pool = context.Pool(workers)

            runs = []
            try:
                for _ in range(config.bench.repeats):
                    runs.append(run_once(config, paths, workers, stage, pool))
            finally:
                if pool is not None:
                    pool.close()
                    pool.join()

            elapsed_times = []
            peak_rss = 0
            digests = set()
            for run in runs:
                elapsed_times.append(run["elapsed_seconds"])
                if run["peak_rss_bytes"] > peak_rss:
                    peak_rss = run["peak_rss_bytes"]
                if run["digest"]:
                    digests.add(run["digest"])

            elapsed = statistics.median(elapsed_times)
            events = runs[0]["events"]

            rows.append(
                {
                    "stage": stage,
                    "workers": workers,
                    "events": events,
                    "median_seconds": round(elapsed, 3),
                    "events_per_second": round(events / elapsed),
                    "peak_rss_mb": round(peak_rss / 2**20, 1),
                    "digests": sorted(digests),
                    "records": runs[0]["records"],
                }
            )

    # Speed-up and efficiency are relative to the first (1-worker) row.
    for stage in ("parse", "full"):
        stage_rows = []
        for row in rows:
            if row["stage"] == stage:
                stage_rows.append(row)

        base = stage_rows[0]["events_per_second"]
        for row in stage_rows:
            row["speedup"] = round(row["events_per_second"] / base, 2)
            row["efficiency"] = round(row["speedup"] / row["workers"], 2)

    # The full pipeline should give one digest no matter how many workers.
    full_digests = set()
    for row in rows:
        if row["stage"] == "full":
            for digest in row["digests"]:
                full_digests.add(digest)

    partition_names = []
    for path in paths:
        partition_names.append(path.name)

    return {
        "partitions": partition_names,
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

    # stats.stats maps (file, line, function) to
    # (primitive calls, total calls, own time, cumulative time, callers).
    raw_stats = stats.stats  # type: ignore[attr-defined]

    total_own_time = 0
    for entry in raw_stats.values():
        total_own_time += entry[2]

    def own_time(item: tuple) -> float:
        return item[1][2]

    top = sorted(raw_stats.items(), key=own_time, reverse=True)
    top = top[: config.bench.profile_top_functions]

    rows = []
    for location, entry in top:
        file_name, line, function_name = location
        calls = entry[1]
        own = entry[2]
        cumulative = entry[3]

        own_share = None
        if total_own_time:
            own_share = round(own / total_own_time, 4)

        rows.append(
            {
                "function": f"{Path(file_name).name}:{line}({function_name})",
                "calls": calls,
                "own_seconds": round(own, 3),
                "own_share": own_share,
                "cumulative_seconds": round(cumulative, 3),
            }
        )

    summary = {
        "elapsed_seconds": round(result["elapsed_seconds"], 3),
        "top": rows,
    }
    return summary, buffer.getvalue()
