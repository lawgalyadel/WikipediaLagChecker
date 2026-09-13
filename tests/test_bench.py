from pathlib import Path

from wikilag.bench import _merge, parse_partition
from wikilag.config import load_config
from wikilag.events import EventCounts

CONFIG = load_config(Path("config/default.toml"))


def test_merge_sums_counts_from_workers():
    total = EventCounts(seen=3, malformed=1, filtered={"type:log": 2})
    _merge(total, EventCounts(seen=5, malformed=0, filtered={"type:log": 1, "wiki": 4}))
    assert (total.seen, total.malformed) == (8, 1)
    assert total.filtered == {"type:log": 3, "wiki": 4}


def test_worker_output_matches_the_serial_filter(tmp_path):
    import gzip
    import json

    rows = [
        {
            "meta": {"id": "a"},
            "wiki": "enwiki",
            "title": "T",
            "namespace": 0,
            "type": "edit",
            "timestamp": 1,
            "bot": False,
        },
        {
            "meta": {"id": "b"},
            "wiki": "enwiki",
            "title": "T",
            "namespace": 0,
            "type": "log",
            "timestamp": 2,
            "bot": False,
        },
    ]
    path = tmp_path / "p.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.writelines(json.dumps(row) + "\n" for row in rows)

    edits, counts = parse_partition((str(path), CONFIG))
    assert [e.event_id for e in edits] == ["a"]
    assert counts.filtered == {"type:log": 1}
