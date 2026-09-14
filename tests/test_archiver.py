import gzip
import zlib
from datetime import UTC, datetime

from WikipediaLagChecker.archiver import (
    ArchiveWriter,
    event_timestamp,
    partition_path,
    read_offset,
    write_offset,
)


def test_partition_path_keys_on_the_hour(tmp_path):
    when = datetime(2026, 9, 13, 14, 37, tzinfo=UTC)
    path = partition_path(tmp_path, "%Y-%m-%d-%H", when)
    assert path.name == "2026-09-13-14.jsonl.gz"


def test_writer_rotates_across_the_hour_boundary(tmp_path):
    early = datetime(2026, 9, 13, 14, 59, tzinfo=UTC)
    late = datetime(2026, 9, 13, 15, 0, tzinfo=UTC)

    with ArchiveWriter(tmp_path, "%Y-%m-%d-%H") as writer:
        writer.write(early, '{"a": 1}')
        writer.write(late, '{"a": 2}')

    assert {p.name for p in tmp_path.iterdir()} == {
        "2026-09-13-14.jsonl.gz",
        "2026-09-13-15.jsonl.gz",
    }


def test_writer_appends_within_a_partition(tmp_path):
    when = datetime(2026, 9, 13, 14, 0, tzinfo=UTC)
    with ArchiveWriter(tmp_path, "%Y-%m-%d-%H") as writer:
        writer.write(when, '{"a": 1}')
        writer.write(when, '{"a": 2}')

    with gzip.open(tmp_path / "2026-09-13-14.jsonl.gz", "rt") as handle:
        assert handle.read().splitlines() == ['{"a": 1}', '{"a": 2}']


def test_flushed_events_are_readable_while_writer_is_open(tmp_path):
    # Simulates a hard kill: the handle is never closed, so only flushed
    # bytes survive. The offset may only advance past what this can read.
    when = datetime(2026, 9, 13, 14, 0, tzinfo=UTC)
    writer = ArchiveWriter(tmp_path, "%Y-%m-%d-%H")
    writer.write(when, '{"a": 1}')
    writer.write(when, '{"a": 2}')
    writer.flush()

    raw = (tmp_path / "2026-09-13-14.jsonl.gz").read_bytes()
    decoded = zlib.decompressobj(wbits=31).decompress(raw).decode("utf-8")
    assert decoded.splitlines() == ['{"a": 1}', '{"a": 2}']
    writer.close()


def test_offset_round_trips(tmp_path):
    offset_file = tmp_path / "offset.txt"
    assert read_offset(offset_file) is None
    write_offset(offset_file, "[{'offset': 12}]")
    assert read_offset(offset_file) == "[{'offset': 12}]"


def test_offset_write_leaves_no_temp_file(tmp_path):
    offset_file = tmp_path / "offset.txt"
    write_offset(offset_file, "x")
    assert [p.name for p in tmp_path.iterdir()] == ["offset.txt"]


def test_event_timestamp_prefers_payload_time():
    event = {"meta": {"dt": "2026-09-13T14:37:00Z"}}
    assert event_timestamp(event).hour == 14


def test_event_timestamp_falls_back_when_unparseable():
    # Malformed timestamps are counted, not dropped.
    assert event_timestamp({"meta": {"dt": "not-a-date"}}) is not None
