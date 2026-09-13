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
# Gzip member header: magic bytes plus the deflate compression method.
GZIP_MAGIC = b"\x1f\x8b\x08"


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
    """Non-empty lines from a partition, tolerating killed gzip members.

    The archiver appends one gzip member per open. A hard kill leaves that
    member without its end-of-stream marker, and a restart then appends a
    fresh member after it, so a damaged member can sit mid-file as well as
    at the end. `gzip.open` raises on either. Here each member is decoded
    on its own; a damaged one yields what was flushed, then decoding
    resumes at the next gzip header.
    """
    data = partition.read_bytes()
    start = 0
    while start < len(data):
        end = yield from _read_member(data, start)
        if end is not None:
            start = end
            continue
        log.warning("partition.damaged_member", partition=partition.name, at=start)
        resync = data.find(GZIP_MAGIC, start + 1)
        if resync == -1:
            return
        start = resync


def _read_member(data: bytes, start: int) -> Iterator[str]:
    """Yield lines of the gzip member at `start`.

    Returns the offset just past the member, or None if it was cut off or
    corrupt. A partial final line is still yielded so the caller counts it
    as undecodable rather than it vanishing.
    """
    decoder = zlib.decompressobj(wbits=GZIP_WBITS)
    pending = b""
    position = start

    while position < len(data) and not decoder.eof:
        chunk = data[position : position + io.DEFAULT_BUFFER_SIZE]
        checkpoint = decoder.copy()
        try:
            output = decoder.decompress(chunk)
        except zlib.error:
            # The exception discards output decoded before the bad byte,
            # which includes events flushed just before the kill. Replay
            # this chunk a byte at a time to recover them.
            decoder = checkpoint
            output = b""
            for index in range(len(chunk)):
                try:
                    output += decoder.decompress(chunk[index : index + 1])
                except zlib.error:
                    break
            yield from _complete_lines(pending + output)
            return None
        position += len(chunk) - len(decoder.unused_data)
        pending += output
        *complete, pending = pending.split(b"\n")
        yield from _complete_lines(b"\n".join(complete))

    yield from _complete_lines(pending)
    return position if decoder.eof else None


def _complete_lines(block: bytes) -> Iterator[str]:
    for raw in block.split(b"\n"):
        if raw.strip():
            yield raw.decode("utf-8", errors="replace").strip()


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
