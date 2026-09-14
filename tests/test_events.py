from pathlib import Path

from WikipediaLagChecker.config import load_config
from WikipediaLagChecker.events import EventCounts, RecentIds, edits, to_edit

CONFIG = load_config(Path("config/default.toml"))


def raw(event_id="e1", wiki="enwiki", title="Tokyo", **overrides):
    event = {
        "meta": {"id": event_id, "dt": "2026-09-13T15:00:02Z"},
        "wiki": wiki,
        "title": title,
        "namespace": 0,
        "type": "edit",
        "timestamp": 1789311600,
        "bot": False,
        "user": "Someone",
        "revision": {"old": 1, "new": 2},
    }
    event.update(overrides)
    return event


def test_to_edit_reads_the_edit_time_not_the_stream_time():
    edit = to_edit(raw())
    assert edit.timestamp == 1789311600
    assert edit.key == ("enwiki", "Tokyo")
    assert edit.revision == 2


def test_to_edit_rejects_payloads_missing_required_fields():
    broken = raw()
    del broken["title"]
    assert to_edit(broken) is None


def test_filters_are_counted_by_reason():
    counts = EventCounts()
    events = [
        raw("1"),
        raw("2", wiki="xxwiki"),
        raw("3", namespace=1),
        raw("4", type="log"),
        {"meta": {}},
    ]
    kept = list(edits(events, CONFIG, counts))

    assert [e.event_id for e in kept] == ["1"]
    assert counts.filtered == {"wiki": 1, "namespace": 1, "type:log": 1}
    assert (counts.seen, counts.kept, counts.malformed) == (5, 1, 1)


def test_bots_are_kept_for_later_accounting():
    assert [e.bot for e in edits([raw(bot=True)], CONFIG)] == [True]


def test_duplicates_from_restarts_are_dropped():
    counts = EventCounts()
    kept = list(edits([raw("1"), raw("2"), raw("1")], CONFIG, counts))
    assert [e.event_id for e in kept] == ["1", "2"]
    assert counts.duplicate == 1


def test_dedupe_window_is_bounded():
    recent = RecentIds(capacity=2)
    assert not recent.seen_before("a")
    assert not recent.seen_before("b")
    assert not recent.seen_before("c")  # evicts "a"
    assert not recent.seen_before("a")
    assert recent.seen_before("c")
