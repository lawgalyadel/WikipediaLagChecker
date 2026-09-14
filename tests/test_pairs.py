import csv

from WikipediaLagChecker.events import Edit
from WikipediaLagChecker.pairs import (
    allocate,
    build_pairs,
    evaluate,
    key_path,
    page_url,
    parse_label,
    sample_for_labelling,
    write_labelling_files,
)


def edit(wiki, title, bot=False):
    return Edit("id", wiki, title, 0, bot, "edit", "U", None)


def test_naive_and_wikidata_pairs_disagree_where_expected():
    resolved = [
        (edit("enwiki", "Paris"), "Q90"),
        (edit("frwiki", "Paris"), "Q90"),  # both methods agree
        (edit("jawiki", "パリ"), "Q90"),  # only Wikidata links the script change
        (edit("enwiki", "Gift"), "Q1"),
        (edit("dewiki", "Gift"), "Q2"),  # same spelling, different subject
    ]
    pairs = build_pairs(resolved)
    strata = pairs.strata()

    assert strata["both"] == [(("enwiki", "Paris"), ("frwiki", "Paris"))]
    assert strata["naive_only"] == [(("dewiki", "Gift"), ("enwiki", "Gift"))]
    assert sorted(strata["wikidata_only"]) == [
        (("enwiki", "Paris"), ("jawiki", "パリ")),
        (("frwiki", "Paris"), ("jawiki", "パリ")),
    ]
    assert (pairs.pages, pairs.pages_with_item) == (5, 5)


def test_bot_only_pages_are_outside_the_universe():
    pairs = build_pairs(
        [(edit("enwiki", "X", bot=True), "Q1"), (edit("dewiki", "X"), "Q1")]
    )
    assert pairs.pages == 1
    assert not pairs.naive and not pairs.wikidata


def test_allocation_is_even_and_redistributes_small_strata():
    assert allocate({"both": 500, "naive_only": 500, "wikidata_only": 500}, 200) == {
        "both": 66,
        "naive_only": 67,
        "wikidata_only": 67,
    }
    assert allocate({"both": 500, "naive_only": 10, "wikidata_only": 500}, 200) == {
        "both": 95,
        "naive_only": 10,
        "wikidata_only": 95,
    }
    assert (
        sum(allocate({"both": 3, "naive_only": 2, "wikidata_only": 0}, 200).values()) == 5
    )


def test_sampling_is_deterministic_for_a_seed():
    resolved = [
        (edit(w, f"T{i}"), f"Q{i}") for i in range(30) for w in ("enwiki", "dewiki")
    ]
    pairs = build_pairs(resolved)
    first = sample_for_labelling(pairs, 10, seed=7)
    second = sample_for_labelling(pairs, 10, seed=7)
    assert first == second


def test_labelling_sheet_is_blind(tmp_path):
    resolved = [
        (edit(w, f"T{i}"), f"Q{i}") for i in range(5) for w in ("enwiki", "dewiki")
    ]
    rows, key, _ = sample_for_labelling(build_pairs(resolved), 5, seed=1)
    labels = tmp_path / "pairs.csv"
    write_labelling_files(labels, rows, key)

    header = labels.read_text(encoding="utf-8").splitlines()[0]
    assert "stratum" not in header and "qid" not in header
    assert key_path(labels).exists()


def test_page_url_is_language_host_and_underscored():
    assert page_url(("zhwiki", "東京 都")) == (
        "https://zh.wikipedia.org/wiki/%E6%9D%B1%E4%BA%AC_%E9%83%BD"
    )


def test_label_parsing_accepts_common_spellings_and_rejects_blanks():
    assert parse_label(" Yes ") is True
    assert parse_label("n") is False
    assert parse_label("") is None
    assert parse_label("maybe") is None


def test_evaluation_reweights_by_stratum_size(tmp_path):
    labels = tmp_path / "pairs.csv"
    # 2 labelled per stratum. both: 2/2 true, naive_only: 0/2, wikidata_only: 1/2.
    sheet = [
        ("p1", "both", "y"),
        ("p2", "both", "y"),
        ("p3", "naive_only", "n"),
        ("p4", "naive_only", "n"),
        ("p5", "wikidata_only", "y"),
        ("p6", "wikidata_only", "n"),
        ("p7", "both", ""),  # unlabelled rows are ignored and counted
    ]
    with labels.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["pair_id", "same_subject"])
        writer.writerows((pid, label) for pid, _, label in sheet)
    with key_path(labels).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["pair_id", "stratum"])
        writer.writerows((pid, stratum) for pid, stratum, _ in sheet)

    sizes = {"both": 100, "naive_only": 50, "wikidata_only": 40}
    result = evaluate(labels, sizes)

    # True estimates: both 100, naive_only 0, wikidata_only 20. Pooled 120.
    assert result["naive"] == {
        "pairs": 150,
        "estimated_true_pairs": 100.0,
        "precision": round(100 / 150, 4),
        "pooled_recall": round(100 / 120, 4),
    }
    assert result["wikidata"]["precision"] == round(120 / 140, 4)
    assert result["wikidata"]["pooled_recall"] == 1.0
    assert result["true_pairs_missed_by_naive"] == 20.0
    assert result["unlabelled"] == 1
