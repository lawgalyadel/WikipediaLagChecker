"""Deterministic replay over archived partitions.

Every experiment from day two onward runs through here rather than against
the live stream. Partitions are read in sorted filename order and lines in
file order, so the same archive produces the same event sequence on every
run — which is what makes a before/after measurement mean anything.
"""

from __future__ import annotations

import io
import json
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

# zlib window-bits value that selects the gzip container format.
GZIP_WBITS = 31


@dataclass(frozen=True)
class ReplayStats:
    partitions: int
    events: int
    undecodable: int

    @property
    def undecodable_rate(self) -> float:
        total = self.events + self.undecodable
        return self.undecodable / total if total else 0.0


def partitions(directory: Path) -> list[Path]:
    """Archive partitions in deterministic order."""
    return sorted(directory.glob("*.jsonl.gz"))


def read_lines(partition: Path) -> Iterator[str]:
    """Non-empty lines from a partition, tolerating a truncated tail.

    The archiver appends one gzip member per open, and a hard kill leaves
    the last member without its end-of-stream marker. `gzip.open` raises on
    that; decoding member by member keeps everything that was flushed.
    """
    decoder = zlib.decompressobj(wbits=GZIP_WBITS)
    in_member = False
    pending = b""

    with partition.open("rb") as handle:
        while chunk := handle.read(io.DEFAULT_BUFFER_SIZE):
            while chunk:
                try:
                    pending += decoder.decompress(chunk)
                except zlib.error:
                    log.warning("partition.corrupt_tail", partition=partition.name)
                    break
                in_member = not decoder.eof
                chunk = decoder.unused_data
                if decoder.eof:
                    decoder = zlib.decompressobj(wbits=GZIP_WBITS)
            else:
                *complete, pending = pending.split(b"\n")
                for raw in complete:
                    if raw.strip():
                        yield raw.decode("utf-8", errors="replace").strip()
                continue
            break

    if in_member:
        log.warning("partition.truncated", partition=partition.name)
    if pending.strip():
        # Unterminated final line; the caller counts it if it won't decode.
        yield pending.decode("utf-8", errors="replace").strip()


def replay(directory: Path) -> Iterator[dict]:
    """Yield decoded events in archive order.

    Undecodable lines are skipped rather than raising: a truncated final
    line is the normal result of killing the archiver mid-write, and one
    bad line should not make a five-day archive unreadable.
    """
    for partition in partitions(directory):
        for line in read_lines(partition):
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def describe(directory: Path) -> ReplayStats:
    """Count what is in the archive, including what failed to decode.

    The undecodable count feeds the drop table in the README. Reporting
    zero is fine; not knowing is not.
    """
    events = 0
    undecodable = 0
    files = partitions(directory)

    for partition in files:
        for line in read_lines(partition):
            try:
                json.loads(line)
            except json.JSONDecodeError:
                undecodable += 1
            else:
                events += 1

    return ReplayStats(partitions=len(files), events=events, undecodable=undecodable)
