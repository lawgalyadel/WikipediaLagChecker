import itertools

from WikipediaLagChecker.analysis import LagAggregator
from WikipediaLagChecker.config import JoinConfig
from WikipediaLagChecker.events import Edit
from WikipediaLagChecker.propagation import PropagationJoin

_ids = itertools.count()


def edit(wiki, ts, title="T", bot=False, user="U"):
    return Edit(
        event_id=f"e{next(_ids)}",
        wiki=wiki,
        title=title,
        timestamp=ts,
        bot=bot,
        change_type="edit",
        user=user,
        revision=None,
    )


def make_join(**overrides):
    values = {
        "watermark_seconds": 100,
        "reorder_seconds": 10,
        "warmup_seconds": 0,
        "emit_ranks": [2, 3, 5],
        "late_horizon_seconds": 1000,
        "min_pair_samples": 1,
    }
    values.update(overrides)
    records, closed = [], []
    join = PropagationJoin(JoinConfig(**values), records.append, closed.append)
    return join, records, closed


def run(join, pushes):
    for e, qid in pushes:
        join.push(e, qid)
    join.finish()


def test_records_are_emitted_at_the_configured_ranks_only():
    join, records, _ = make_join()
    wikis = ["enwiki", "dewiki", "frwiki", "jawiki", "eswiki", "itwiki"]
    run(join, [(edit(w, 10 * i), "Q1") for i, w in enumerate(wikis)])

    assert [(r.rank, r.follower_wiki, r.lag_seconds) for r in records] == [
        (2, "dewiki", 10),
        (3, "frwiki", 20),
        (5, "eswiki", 40),
    ]
    assert {r.leader_wiki for r in records} == {"enwiki"}


def test_repeat_edits_in_one_edition_do_not_advance_the_rank():
    join, records, _ = make_join()
    run(
        join,
        [(edit("enwiki", 0), "Q1"), (edit("enwiki", 5), "Q1"), (edit("dewiki", 9), "Q1")],
    )
    assert [(r.rank, r.lag_seconds) for r in records] == [(2, 9)]


def test_arrival_disorder_inside_the_buffer_is_corrected():
    # dewiki's edit arrives first but was made later.
    join, records, _ = make_join(reorder_seconds=10)
    run(join, [(edit("dewiki", 105), "Q1"), (edit("enwiki", 100), "Q1")])

    (record,) = records
    assert (record.leader_wiki, record.follower_wiki, record.lag_seconds) == (
        "enwiki",
        "dewiki",
        5,
    )
    assert join.stats.out_of_order == 0


def test_disorder_beyond_the_buffer_is_counted_not_hidden():
    join, _, _ = make_join(reorder_seconds=10)
    # dewiki@150 releases enwiki@100; frwiki@90 then arrives too late to
    # be placed before it.
    run(
        join,
        [
            (edit("enwiki", 100), "Q1"),
            (edit("dewiki", 150), "Q2"),
            (edit("frwiki", 90), "Q3"),
        ],
    )
    assert join.stats.out_of_order == 1


def test_editions_after_the_watermark_are_late_followups_not_records():
    join, records, closed = make_join(watermark_seconds=100)
    run(
        join,
        [
            (edit("enwiki", 0), "Q1"),
            (edit("frwiki", 50), "Q2"),
            (edit("dewiki", 150), "Q1"),  # Q1's window closed at 100
            (edit("jawiki", 160), "Q1"),  # and Q2's at 150
        ],
    )

    assert records == []
    assert join.stats.late_followup_items == 1
    assert join.stats.late_followup_edits == 2
    assert join.stats.closed_with_human_edit == 2
    assert join.stats.late_followup_rate == 0.5
    assert [w.qid for w in closed] == ["Q1", "Q2"]


def test_late_edits_do_not_reopen_the_window():
    join, records, _ = make_join(watermark_seconds=100)
    run(
        join,
        [
            (edit("enwiki", 0), "Q1"),
            (edit("dewiki", 150), "Q1"),
            (edit("frwiki", 160), "Q1"),
        ],
    )
    assert records == []  # dewiki must not become a leader for frwiki


def test_bots_neither_lead_nor_follow_but_count_toward_bot_share():
    join, records, _ = make_join(watermark_seconds=100)
    run(
        join,
        [
            (edit("dewiki", 0, bot=True), "Q1"),
            (edit("enwiki", 10), "Q1"),
            (edit("frwiki", 20), "Q1"),
            (edit("svwiki", 30, bot=True), "Q1"),
            (edit("enwiki", 500), "Q9"),  # advances the clock past Q1's window
        ],
    )

    assert [(r.leader_wiki, r.follower_wiki, r.lag_seconds) for r in records] == [
        ("enwiki", "frwiki", 10)
    ]
    assert join.stats.cross_edition_items == 1
    assert join.stats.bot_share_of_cross_edition_edits == 0.5


def test_warmup_windows_emit_nothing_and_are_excluded_from_rates():
    join, records, _ = make_join(warmup_seconds=50, watermark_seconds=100)
    run(
        join,
        [
            (edit("enwiki", 0), "Q1"),
            (edit("dewiki", 5), "Q1"),
            (edit("enwiki", 60), "Q2"),
            (edit("dewiki", 70), "Q2"),
            (edit("enwiki", 400), "Q3"),
        ],
    )

    assert [r.qid for r in records] == ["Q2"]
    assert join.stats.warmup_windows == 1
    assert join.stats.closed_with_human_edit == 1


def test_windows_open_at_the_end_are_counted_but_not_rated():
    join, _, _ = make_join(watermark_seconds=100)
    run(join, [(edit("enwiki", 0), "Q1")])
    assert join.stats.windows_open_at_end == 1
    assert join.stats.closed_with_human_edit == 0
    assert join.stats.late_followup_rate is None


def test_state_stays_bounded_over_a_long_stream():
    join, _, _ = make_join(
        watermark_seconds=100, late_horizon_seconds=300, reorder_seconds=0
    )
    run(join, [(edit("enwiki", ts), f"Q{ts}") for ts in range(0, 10_000, 5)])

    # One item every 5s: at most watermark/5 open, horizon/5 remembered.
    assert join.stats.peak_open_windows <= 100 // 5 + 1
    assert join.stats.peak_closed_remembered <= 300 // 5 + 1


def test_edits_without_an_item_are_counted_and_skipped():
    join, records, _ = make_join()
    run(join, [(edit("enwiki", 0), None), (edit("dewiki", 1), None)])
    assert records == []
    assert join.stats.without_item == 2


def test_timestamp_ties_resolve_by_arrival_order():
    join, records, _ = make_join()
    run(join, [(edit("jawiki", 50), "Q1"), (edit("enwiki", 50), "Q1")])
    assert records[0].leader_wiki == "jawiki"


def test_rank_reach_counts_only_closed_windows():
    join, _, _ = make_join(watermark_seconds=100)
    run(
        join,
        [
            (edit("enwiki", 0), "Q1"),
            (edit("dewiki", 1), "Q1"),
            (edit("frwiki", 2), "Q1"),
            (edit("enwiki", 3), "Q2"),
            (edit("enwiki", 500), "Q3"),
        ],
    )
    assert join.stats.reached_rank == {2: 1, 3: 1, 5: 0}
    assert join.stats.closed_with_human_edit == 2


def test_lead_lift_corrects_for_how_many_editions_were_present():
    aggregator = LagAggregator()
    join, _, _ = make_join(watermark_seconds=100, min_pair_samples=1)
    join._on_record = aggregator.add_record
    join._on_close = aggregator.add_window
    pushes = []
    for i in range(4):  # enwiki always leads a two-edition window
        base = i * 1000
        pushes += [(edit("enwiki", base), f"A{i}"), (edit("dewiki", base + 10), f"A{i}")]
    pushes.append((edit("enwiki", 10_000), "END"))
    run(join, pushes)

    editions = {row["wiki"]: row for row in aggregator.summary(min_samples=1)["editions"]}
    assert editions["enwiki"]["lead_rate"] == 1.0
    assert editions["enwiki"]["expected_lead_rate"] == 0.5
    assert editions["enwiki"]["lead_lift"] == 2.0
    assert editions["dewiki"]["median_follow_lag_seconds"] == 10
    summary = aggregator.summary(min_samples=1)
    assert summary["lag_by_pair"][0] == {
        "leader": "enwiki",
        "follower": "dewiki",
        "n": 4,
        "median_seconds": 10,
        "p95_seconds": 10,
    }
