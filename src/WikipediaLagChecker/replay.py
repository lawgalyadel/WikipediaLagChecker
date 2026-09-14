"""Deterministic replay over archived partitions.

All analysis reads from here instead of the live stream. Partitions are
read in sorted filename order and lines in file order, so the same archive
gives the same event sequence on every run. Without that, before/after
comparisons wouldn't be meaningful.
"""

from __future__ import annotations

import io
import json
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
    damaged_members: int = 0
    first_event: str | None = None  # ISO minute
    last_event: str | None = None
    # Runs of whole minutes with no archived event, as (start, end, minutes).
    gaps: list[tuple[str, str, int]] = field(default_factory=list)

    @property
    def undecodable_rate(self) -> float:
        total = self.events + self.undecodable
        if total == 0:
            return 0.0
        return self.undecodable / total


def partitions(directory: Path) -> list[Path]:
    """Archive partitions in deterministic order."""
    files = directory.glob("*.jsonl.gz")
    return sorted(files)


def read_lines(partition: Path, damaged: list[int] | None = None) -> Iterator[str]:
    """Non-empty lines from a partition, tolerating killed gzip members.

    The archiver appends one gzip member per open. A hard kill leaves that
    member without its end-of-stream marker, and a restart then appends a
    fresh member after it, so a damaged member can sit mid-file as well as
    at the end. `gzip.open` raises on either. Here each member is decoded
    on its own; a damaged one yields what was flushed, then decoding
    resumes at the next gzip header. Byte offsets of damaged members are
    appended to `damaged` when given, so they can be counted.
    """
    data = partition.read_bytes()
    start = 0

    while start < len(data):
        # _read_member yields the lines of one member, and its return value
        # is where that member ended (None if it was damaged). `yield from`
        # passes the lines straight through and gives us the return value.
        end = yield from _read_member(data, start)

        if end is not None:
            start = end
            continue

        log.warning("partition.damaged_member", partition=partition.name, at=start)
        if damaged is not None:
            damaged.append(start)

        # Skip ahead to the next gzip header, if there is one.
        next_header = data.find(GZIP_MAGIC, start + 1)
        if next_header == -1:
            return
        start = next_header


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
                single_byte = chunk[index : index + 1]
                try:
                    output += decoder.decompress(single_byte)
                except zlib.error:
                    break

            for line in _complete_lines(pending + output):
                yield line
            return None

        # Only count the bytes the decoder actually used for this member.
        used_bytes = len(chunk) - len(decoder.unused_data)
        position += used_bytes

        # Everything up to the last newline is complete; keep the rest.
        pending += output
        pieces = pending.split(b"\n")
        pending = pieces[-1]
        complete_block = b"\n".join(pieces[:-1])
        for line in _complete_lines(complete_block):
            yield line

    for line in _complete_lines(pending):
        yield line

    if decoder.eof:
        return position
    return None


def _complete_lines(block: bytes) -> Iterator[str]:
    for raw in block.split(b"\n"):
        if raw.strip():
            text = raw.decode("utf-8", errors="replace")
            yield text.strip()


def replay(directory: Path) -> Iterator[dict]:
    """Yield decoded events in archive order.

    Undecodable lines are skipped rather than raising: a truncated final
    line is the normal result of killing the archiver mid-write, and one
    bad line should not make a five-day archive unreadable.
    """
    all_partitions = partitions(directory)
    yield from replay_partitions(all_partitions)


def replay_partitions(paths: Iterable[Path]) -> Iterator[dict]:
    """`replay` over an explicit, already-ordered list of partitions."""
    for partition in paths:
        for line in read_lines(partition):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield event


def describe(directory: Path, files: list[Path] | None = None) -> ReplayStats:
    """Count what is in the archive, including what failed to decode.

    The undecodable count goes into the Data table in the README. Coverage
    gaps (whole minutes with no event, e.g. from an outage or the laptop
    sleeping) are found from event time, since a gap shortens any
    propagation window that spans it.
    """
    events = 0
    undecodable = 0
    damaged: list[int] = []
    minutes: set[datetime] = set()

    if files is None:
        files = partitions(directory)

    for partition in files:
        for line in read_lines(partition, damaged):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                undecodable += 1
                continue

            events += 1
            minute = _event_minute(event)
            if minute is not None:
                minutes.add(minute)

    first_event = None
    last_event = None
    if len(minutes) > 0:
        first_event = min(minutes).isoformat()
        last_event = max(minutes).isoformat()

    sorted_minutes = sorted(minutes)
    gaps = _gaps(sorted_minutes)

    return ReplayStats(
        partitions=len(files),
        events=events,
        undecodable=undecodable,
        damaged_members=len(damaged),
        first_event=first_event,
        last_event=last_event,
        gaps=gaps,
    )


def _event_minute(event: dict) -> datetime | None:
    if not isinstance(event, dict):
        return None

    meta = event.get("meta")
    if not meta:
        return None

    dt = meta.get("dt")
    if not isinstance(dt, str):
        return None

    try:
        parsed = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    except ValueError:
        return None

    # Round down to the start of the minute.
    return parsed.replace(second=0, microsecond=0)


def _gaps(ordered: list[datetime]) -> list[tuple[str, str, int]]:
    one_minute = timedelta(minutes=1)
    gaps = []

    for index in range(1, len(ordered)):
        previous = ordered[index - 1]
        current = ordered[index]

        # Consecutive minutes are exactly one minute apart; more means a gap.
        if current - previous > one_minute:
            gap_start = previous + one_minute
            gap_end = current - one_minute
            missing_minutes = (current - previous) // one_minute - 1
            gaps.append((gap_start.isoformat(), gap_end.isoformat(), missing_minutes))

    return gaps
