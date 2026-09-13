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
        return self.undecodable / total if total else 0.0


def partitions(directory: Path) -> list[Path]:
    """Archive partitions in deterministic order."""
    return sorted(directory.glob("*.jsonl.gz"))


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
        end = yield from _read_member(data, start)
        if end is not None:
            start = end
            continue
        log.warning("partition.damaged_member", partition=partition.name, at=start)
        if damaged is not None:
            damaged.append(start)
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
    yield from replay_partitions(partitions(directory))


def replay_partitions(paths: Iterable[Path]) -> Iterator[dict]:
    """`replay` over an explicit, already-ordered list of partitions."""
    for partition in paths:
        for line in read_lines(partition):
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def describe(directory: Path, files: list[Path] | None = None) -> ReplayStats:
    """Count what is in the archive, including what failed to decode.

    The undecodable count feeds the drop table in the README. Reporting
    zero is fine; not knowing is not. Coverage gaps — whole minutes with no
    event, from outages or a sleeping laptop — are found from event time,
    because a gap silently shortens every propagation window across it.
    """
    events = 0
    undecodable = 0
    damaged: list[int] = []
    minutes: set[datetime] = set()
    files = partitions(directory) if files is None else files

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

    first = min(minutes) if minutes else None
    last = max(minutes) if minutes else None
    return ReplayStats(
        partitions=len(files),
        events=events,
        undecodable=undecodable,
        damaged_members=len(damaged),
        first_event=first.isoformat() if first else None,
        last_event=last.isoformat() if last else None,
        gaps=_gaps(sorted(minutes)),
    )


def _event_minute(event: dict) -> datetime | None:
    dt = (event.get("meta") or {}).get("dt") if isinstance(event, dict) else None
    if not isinstance(dt, str):
        return None
    try:
        parsed = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(second=0, microsecond=0)


def _gaps(ordered: list[datetime]) -> list[tuple[str, str, int]]:
    step = timedelta(minutes=1)
    gaps = []
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current - previous > step:
            start, end = previous + step, current - step
            gaps.append(
                (start.isoformat(), end.isoformat(), (current - previous) // step - 1)
            )
    return gaps
