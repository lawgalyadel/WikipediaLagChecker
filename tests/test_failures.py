import csv
import dataclasses
from pathlib import Path

from wikilag.config import load_config
from wikilag.failures import (
    EditEvidence,
    build_sheet,
    evidence_from_events,
    suggest,
    summarise_sheet,
    write_sheet,
)

CONFIG = load_config(Path("config/default.toml"))


def record(**overrides):
    base = {
        "qid": "Q1",
        "rank": "2",
        "lag_seconds": "60",
        "leader_wiki": "enwiki",
        "leader_title": "Tokyo",
        "leader_timestamp": "100",
        "follower_wiki": "jawiki",
        "follower_title": "東京都",
        "follower_timestamp": "160",
        "follower_user": "B",
    }
    base.update(overrides)
    return base


def evidence(**overrides):
    base = EditEvidence(
        user="A", comment="expand", bytes_changed=500, bot=False, stream_skew_seconds=2.0
    )
    return dataclasses.replace(base, **overrides)


def test_evidence_is_recovered_from_raw_events():
    events = [
        {
            "wiki": "enwiki",
            "title": "Tokyo",
            "timestamp": 100,
            "user": "A",
            "comment": "update population",
            "length": {"old": 1000, "new": 1042},
            "bot": False,
            "meta": {"dt": "1970-01-01T00:01:43Z"},
        },
        {"wiki": "enwiki", "title": "Other", "timestamp": 100},
    ]
    found = evidence_from_events(events, {("enwiki", "Tokyo", 100)})
    assert found == {
        ("enwiki", "Tokyo", 100): EditEvidence(
            user="A",
            comment="update population",
            bytes_changed=42,
            bot=False,
            stream_skew_seconds=3.0,
        )
    }


def test_page_creation_counts_its_full_size():
    events = [{"wiki": "enwiki", "title": "New", "timestamp": 1, "length": {"new": 300}}]
    found = evidence_from_events(events, {("enwiki", "New", 1)})
    assert found[("enwiki", "New", 1)].bytes_changed == 300


def test_suggestions_fire_in_priority_order():
    cases = [
        (evidence(user="InternetArchiveBot"), evidence(), "bot_slipped_filter"),
        (evidence(), evidence(comment="Undid revision 123 by X"), "revert_or_vandalism"),
        (evidence(bytes_changed=3), evidence(bytes_changed=-2), "trivial_maintenance"),
        (evidence(), evidence(stream_skew_seconds=900.0), "out_of_order"),
        (evidence(user="Same"), evidence(user="Same"), "same_editor"),
        (evidence(), evidence(user="B"), "needs_review"),
    ]
    for leader, follower, expected in cases:
        category, _ = suggest(record(), leader, follower, CONFIG)
        assert category == expected, (leader, follower)


def test_flagged_bots_are_not_reported_as_slipping_the_filter():
    category, _ = suggest(
        record(), evidence(user="SomeBot", bot=True), evidence(), CONFIG
    )
    assert category != "bot_slipped_filter"


def test_disambiguation_is_detected_from_the_title():
    category, why = suggest(
        record(follower_title="Mercury (disambiguation)"), evidence(), evidence(), CONFIG
    )
    assert category == "disambiguation"
    assert "Mercury" in why


def test_missing_evidence_falls_through_without_crashing():
    category, _ = suggest(record(), None, None, CONFIG)
    assert category == "needs_review"


def test_summary_counts_only_confirmed_rows_and_prefers_the_reviewer(tmp_path):
    rows = build_sheet([record(), record(qid="Q2"), record(qid="Q3")], {}, CONFIG)
    rows[0].update(confirmed_wrong="y", category="redirect")
    rows[1].update(confirmed_wrong="y")  # accepts the suggestion
    rows[2].update(confirmed_wrong="n")
    sheet = tmp_path / "failures.csv"
    write_sheet(sheet, rows)

    summary = summarise_sheet(sheet)

    assert (summary["reviewed"], summary["confirmed_wrong"]) == (3, 2)
    assert {c["category"]: c["cases"] for c in summary["categories"]} == {
        "redirect": ["f001"],
        "needs_review": ["f002"],
    }
    assert summary["heuristic_agreement"] == 0.5
    with sheet.open(encoding="utf-8") as handle:
        assert "confirmed_wrong" in next(csv.reader(handle))
