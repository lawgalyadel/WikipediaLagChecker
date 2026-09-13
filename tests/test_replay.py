import gzip
import io
import json

from wikilag.replay import describe, replay


def _write(directory, name, rows):
    directory.mkdir(parents=True, exist_ok=True)
    with gzip.open(directory / name, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(row + "\n")


def test_replay_is_deterministic_across_runs(tmp_path):
    _write(tmp_path, "2026-09-13-15.jsonl.gz", [json.dumps({"i": 3})])
    _write(tmp_path, "2026-09-13-14.jsonl.gz", [json.dumps({"i": i}) for i in (1, 2)])

    first = [event["i"] for event in replay(tmp_path)]
    second = [event["i"] for event in replay(tmp_path)]

    assert first == [1, 2, 3]  # partition order, then line order
    assert first == second


def test_describe_counts_undecodable_lines(tmp_path):
    _write(tmp_path, "2026-09-13-14.jsonl.gz", [json.dumps({"i": 1}), "{truncated"])
    stats = describe(tmp_path)
    assert (stats.events, stats.undecodable) == (1, 1)
    assert stats.undecodable_rate == 0.5


def test_replay_reads_multiple_appended_members(tmp_path):
    # The archiver reopens partitions in append mode, one gzip member each.
    _write(tmp_path, "p.jsonl.gz", [json.dumps({"i": 1})])
    with gzip.open(tmp_path / "p.jsonl.gz", "at", encoding="utf-8") as handle:
        handle.write(json.dumps({"i": 2}) + "\n")
    assert [event["i"] for event in replay(tmp_path)] == [1, 2]


def test_replay_survives_a_hard_killed_partition(tmp_path):
    # Flushed but never closed: no end-of-stream marker, as after a kill.
    path = tmp_path / "p.jsonl.gz"
    writer = gzip.open(path, "wt", encoding="utf-8")
    writer.write(json.dumps({"i": 1}) + "\n" + json.dumps({"i": 2}) + "\n")
    writer.flush()
    killed = path.read_bytes()
    writer.close()
    path.write_bytes(killed)

    assert [event["i"] for event in replay(tmp_path)] == [1, 2]
    assert describe(tmp_path).events == 2


def _killed_member(rows):
    """Bytes of a gzip member flushed but never closed, as after a kill."""
    buffer = io.BytesIO()
    writer = gzip.GzipFile(fileobj=buffer, mode="wb")
    writer.write("".join(row + "\n" for row in rows).encode())
    writer.flush()
    return buffer.getvalue()


def _complete_member(rows):
    return gzip.compress("".join(row + "\n" for row in rows).encode())


def test_replay_continues_past_a_killed_member_mid_file(tmp_path):
    # A restarted archiver appends a fresh member after the killed one.
    rows = [json.dumps({"i": i}) for i in range(4)]
    (tmp_path / "p.jsonl.gz").write_bytes(
        _killed_member(rows[:2]) + _complete_member(rows[2:])
    )
    assert [event["i"] for event in replay(tmp_path)] == [0, 1, 2, 3]


def test_replay_keeps_events_after_several_killed_members(tmp_path):
    rows = [json.dumps({"i": i}) for i in range(600)]
    (tmp_path / "p.jsonl.gz").write_bytes(
        _killed_member(rows[:200])
        + _killed_member(rows[200:400])
        + _complete_member(rows[400:])
    )
    assert [event["i"] for event in replay(tmp_path)] == list(range(600))


def test_truncated_line_does_not_break_replay(tmp_path):
    _write(tmp_path, "2026-09-13-14.jsonl.gz", [json.dumps({"i": 1}), "{truncated"])
    assert [event["i"] for event in replay(tmp_path)] == [1]


def test_describe_reports_coverage_gaps_in_event_time(tmp_path):
    def at(minute):
        return json.dumps({"meta": {"dt": f"2026-09-13T15:{minute:02d}:30Z"}})

    _write(tmp_path, "2026-09-13-15.jsonl.gz", [at(0), at(1), at(5), at(6), at(9)])
    stats = describe(tmp_path)

    assert stats.first_event == "2026-09-13T15:00:00+00:00"
    assert stats.last_event == "2026-09-13T15:09:00+00:00"
    assert stats.gaps == [
        ("2026-09-13T15:02:00+00:00", "2026-09-13T15:04:00+00:00", 3),
        ("2026-09-13T15:07:00+00:00", "2026-09-13T15:08:00+00:00", 2),
    ]


def test_describe_counts_damaged_members(tmp_path):
    (tmp_path / "p.jsonl.gz").write_bytes(
        _killed_member([json.dumps({"i": 0})]) + _complete_member([json.dumps({"i": 1})])
    )
    stats = describe(tmp_path)
    assert (stats.events, stats.damaged_members) == (2, 1)
