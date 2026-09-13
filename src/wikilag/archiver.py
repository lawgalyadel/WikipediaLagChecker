"""Raw event archiver.

This runs from day one and keeps running while the rest of the project is
built. Two responsibilities, both boring on purpose:

1. Write every raw event to disk, partitioned by hour, gzipped. Nothing is
   parsed or filtered here beyond a wiki whitelist. Analysis happens later
   against the archive, never against the live stream, so every experiment
   is reproducible.
2. Persist the last event id so a dropped connection resumes where it left
   off instead of leaving a hole in the data.
"""

from __future__ import annotations

import gzip
import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import structlog

from wikilag.config import Config
from wikilag.sse import SSEMessage, parse_sse

log = structlog.get_logger(__name__)


def partition_path(directory: Path, fmt: str, when: datetime) -> Path:
    """Path for the partition covering `when`.

    Partitions are keyed on the event's own timestamp, not wall clock, so
    a replayed or backfilled event lands in the hour it belongs to.
    """
    return directory / f"{when.strftime(fmt)}.jsonl.gz"


class ArchiveWriter:
    """Append raw event lines to hourly gzip partitions.

    Keeps at most one file handle open and rotates when the hour changes.
    Used as a context manager so the final partition is flushed on exit.
    """

    def __init__(self, directory: Path, partition_format: str) -> None:
        self._directory = directory
        self._format = partition_format
        self._current_key: str | None = None
        self._handle: gzip.GzipFile | None = None

    def write(self, when: datetime, payload: str) -> Path:
        path = partition_path(self._directory, self._format, when)
        key = path.name

        if key != self._current_key:
            self.close()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = gzip.open(path, "at", encoding="utf-8")
            self._current_key = key
            log.info("partition.rotated", partition=key)

        assert self._handle is not None
        self._handle.write(payload.rstrip("\n") + "\n")
        return path

    def flush(self) -> None:
        """Push buffered bytes into the partition file.

        Gzip holds output in memory until flushed. Anything not flushed is
        lost on a hard kill, so the offset must never advance past it.
        """
        if self._handle is not None:
            self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
            self._current_key = None

    def __enter__(self) -> ArchiveWriter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def read_offset(offset_file: Path) -> str | None:
    """Last event id we durably archived, or None for a cold start."""
    if not offset_file.exists():
        return None
    content = offset_file.read_text(encoding="utf-8").strip()
    return content or None


def write_offset(offset_file: Path, event_id: str) -> None:
    """Persist the offset atomically.

    Written to a temp file and renamed so a crash mid-write cannot leave a
    truncated offset that would silently restart the stream from scratch.
    """
    offset_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = offset_file.with_suffix(offset_file.suffix + ".tmp")
    tmp.write_text(event_id, encoding="utf-8")
    tmp.replace(offset_file)


def event_timestamp(event: dict) -> datetime:
    """Event time from the payload, falling back to now.

    The feed carries `meta.dt` as an ISO timestamp. Anything malformed
    falls back to wall clock and is counted, not dropped — an unparseable
    timestamp is a data-quality finding, not a reason to lose the record.
    """
    dt = event.get("meta", {}).get("dt")
    if isinstance(dt, str):
        try:
            return datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            log.warning("timestamp.unparseable", value=dt)
    return datetime.now(UTC)


def should_keep(event: dict, config: Config) -> bool:
    """Wiki and namespace whitelist, applied before anything hits disk.

    Bots are archived regardless of `include_bots`: they are filtered at
    analysis time, because the bot share of cross-edition edits is itself
    a number worth reporting.
    """
    if event.get("wiki") not in config.filters.wikis:
        return False
    return event.get("namespace") in config.filters.namespaces


def stream_messages(config: Config, resume_from: str | None) -> Iterator[SSEMessage]:
    """Connect and yield messages until the connection drops."""
    headers = {"User-Agent": "wikilag/0.1 (student research project)"}
    if resume_from:
        headers["Last-Event-ID"] = resume_from

    timeout = httpx.Timeout(config.stream.read_timeout_seconds, connect=10.0)
    with httpx.stream(
        "GET", config.stream.url, headers=headers, timeout=timeout
    ) as response:
        response.raise_for_status()
        yield from parse_sse(response.iter_lines())


def run_archiver(config: Config, max_events: int | None = None) -> int:
    """Archive loop with exponential backoff on disconnect.

    `max_events` exists so tests and smoke runs can bound the loop. In
    production it is None and this runs until killed.
    """
    backoff = config.stream.reconnect_initial_seconds
    archived = 0
    offset = read_offset(config.archive.offset_file)

    writer = ArchiveWriter(config.archive.directory, config.archive.partition_format)
    with writer:
        while True:
            try:
                for message in stream_messages(config, offset):
                    if message.event != "message":
                        continue

                    try:
                        event = json.loads(message.data)
                    except json.JSONDecodeError:
                        log.warning("event.undecodable", length=len(message.data))
                        continue

                    if not should_keep(event, config):
                        continue

                    writer.write(event_timestamp(event), message.data)
                    if message.id:
                        offset = message.id

                    archived += 1
                    backoff = config.stream.reconnect_initial_seconds

                    # Offset is persisted only after the data behind it is
                    # on disk. A hard kill re-archives up to one flush
                    # interval (at-least-once) rather than leaving a gap.
                    if archived % config.archive.flush_every_events == 0:
                        writer.flush()
                        if offset:
                            write_offset(config.archive.offset_file, offset)

                    if archived % 1000 == 0:
                        log.info("archive.progress", archived=archived)
                    if max_events is not None and archived >= max_events:
                        writer.flush()
                        if offset:
                            write_offset(config.archive.offset_file, offset)
                        return archived

            except (httpx.HTTPError, OSError) as exc:
                log.warning("stream.disconnected", error=str(exc), backoff=backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, config.stream.reconnect_max_seconds)
            else:
                log.info("stream.ended", backoff=backoff)
                time.sleep(backoff)
