import pytest

from wikilag.report import (
    MARK_END,
    MARK_START,
    RESULT_NAMES,
    duration,
    render,
    update_readme,
)


def empty():
    return dict.fromkeys(RESULT_NAMES)


def test_missing_results_render_as_commands_not_numbers():
    rendered = render(empty())
    assert "run `wikilag stats`" in rendered
    assert "run `wikilag join`" in rendered
    assert "run `wikilag bench --label after`" in rendered
    assert "0.0%" not in rendered


def test_join_inside_warmup_says_so_instead_of_reporting_zeros():
    results = empty()
    results["propagation"] = {
        "run_id": "r1",
        "watermark_seconds": 21600,
        "warmup_seconds": 21600,
        "reorder_seconds": 120,
        "join": {
            "closed_with_human_edit": 0,
            "windows_opened": 36068,
            "warmup_windows": 36068,
            "windows_open_at_end": 36068,
        },
        "rates": {},
        "lags": {},
    }
    rendered = render(results)
    assert "No propagation numbers yet" in rendered
    assert "36,068" in rendered
    assert "12h 00m" in rendered


def test_baseline_without_labels_reports_counts_but_not_precision():
    results = empty()
    results["pairs"] = {
        "run_id": "r2",
        "pages": 1000,
        "pages_with_item": 900,
        "naive_pairs": 678,
        "wikidata_pairs": 1867,
        "stratum_sizes": {"both": 656, "naive_only": 22, "wikidata_only": 1211},
        "allocation": {"both": 89, "naive_only": 22, "wikidata_only": 89},
        "seed": 1,
    }
    rendered = render(results)
    assert "| Cross-edition pairs found | 678 | 1,867 |" in rendered
    assert "run `wikilag pairs evaluate`" in rendered
    assert "1,211 pairs are found only by Wikidata" in rendered


def test_durations_are_human_readable():
    assert duration(45) == "45s"
    assert duration(685) == "11m 25s"
    assert duration(21600) == "6h 00m"
    assert duration(None) == "—"


def test_update_replaces_only_between_markers(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text(f"intro\n{MARK_START}\nold\n{MARK_END}\noutro\n", encoding="utf-8")

    assert update_readme(readme, "new") is True
    assert readme.read_text(encoding="utf-8") == (
        f"intro\n{MARK_START}\n\nnew\n\n{MARK_END}\noutro\n"
    )
    assert update_readme(readme, "new") is False


def test_update_refuses_a_readme_without_markers(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("no markers", encoding="utf-8")
    with pytest.raises(ValueError):
        update_readme(readme, "new")
