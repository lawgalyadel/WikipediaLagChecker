"""Render the README results section from `results/*.json`.

Every number in the results block comes from a results file written by a
run. If a result doesn't exist yet, the command that produces it is shown
instead. Everything between the markers gets replaced on each run, so don't
edit that part of the README by hand.
"""

from __future__ import annotations

import json
from pathlib import Path

MARK_START = "<!-- results:start -->"
MARK_END = "<!-- results:end -->"

RESULT_NAMES = (
    "archive",
    "resolve",
    "pairs",
    "baseline",
    "propagation",
    "bench_before",
    "bench_after",
    "profile_before",
    "profile_after",
    "failures_sample",
    "failures",
)
TOP_PAIRS = 15


def load_results(directory: Path) -> dict[str, dict | None]:
    """Every known result file, or None for the ones that don't exist yet."""
    loaded: dict[str, dict | None] = {}
    for name in RESULT_NAMES:
        path = directory / f"{name}.json"
        if path.exists():
            text = path.read_text(encoding="utf-8")
            loaded[name] = json.loads(text)
        else:
            loaded[name] = None
    return loaded


# --- formatting --------------------------------------------------------------


def num(value: float | int | None) -> str:
    """1234 -> "1,234", 12.34 -> "12.3", None -> "-"."""
    if value is None:
        return "-"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.1f}"
    return f"{int(value):,}"


def pct(value: float | None, digits: int = 1) -> str:
    """0.123 -> "12.3%"."""
    if value is None:
        return "-"
    percentage = value * 100
    return f"{percentage:.{digits}f}%"


def duration(seconds: float | None) -> str:
    """Seconds as "45s", "11m 25s" or "6h 00m"."""
    if seconds is None:
        return "-"

    seconds = round(seconds)
    if seconds < 90:
        return f"{seconds}s"

    minutes = seconds // 60
    rest = seconds % 60
    if minutes < 90:
        return f"{minutes}m {rest:02d}s"

    hours = minutes // 60
    minutes = minutes % 60
    return f"{hours}h {minutes:02d}m"


def plural(count: int, noun: str) -> str:
    if count == 1:
        return f"{num(count)} {noun}"
    return f"{num(count)} {noun}s"


def pending(command: str) -> str:
    return f"_pending (run `{command}`)_"


def table(headers: list[str], rows: list[list[str]]) -> str:
    """A markdown table."""
    header_line = "| " + " | ".join(headers) + " |"

    separators = []
    for _ in headers:
        separators.append("---")
    separator_line = "|" + "|".join(separators) + "|"

    lines = [header_line, separator_line]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines)


def _share(part: int | None, whole: int | None) -> float | None:
    if part is None or not whole:
        return None
    return part / whole


# --- sections ----------------------------------------------------------------


def section_data(r: dict) -> str:
    archive = r["archive"]
    resolve = r["resolve"]

    if archive is None:
        return "### Data\n\n" + pending("python -m WikipediaLagChecker stats")

    # Biggest gaps first; the top three are listed.
    def gap_length(gap: list) -> int:
        return -gap[2]

    gaps = sorted(archive["gaps"], key=gap_length)

    largest_parts = []
    for gap in gaps[:3]:
        start = gap[0]
        minutes = gap[2]
        largest_parts.append(f"{minutes} min from {start[11:16]} UTC {start[:10]}")
    largest = ", ".join(largest_parts)

    window = (
        f"{archive['first_event'][:16]} → {archive['last_event'][:16]}"
        f" ({archive['partitions']} hourly partitions)"
    )
    undecodable = f"{num(archive['undecodable'])} ({pct(archive['undecodable_rate'], 4)})"
    coverage = f"{plural(len(gaps), 'gap')}, {plural(archive['gap_minutes'], 'minute')}"
    if len(gaps) > 0:
        coverage += f"; largest: {largest}"

    rows = [
        ["Archive window (event time, UTC)", window],
        ["Raw events archived", num(archive["events"])],
        ["Undecodable archive lines", undecodable],
        ["Damaged gzip members recovered", num(archive["damaged_members"])],
        ["Coverage gaps", coverage],
    ]

    if resolve is not None:
        events = resolve["events"]

        filtered_parts = []
        for reason, count in sorted(events["filtered"].items()):
            filtered_parts.append(f"{reason} {num(count)}")
        filtered_out = ", ".join(filtered_parts)
        if filtered_out == "":
            filtered_out = "0"

        rows.append(["Article edits after filtering", num(events["kept"])])
        rows.append(
            ["Duplicates removed (restart re-archiving)", num(events["duplicate"])]
        )
        rows.append(["Filtered out", filtered_out])

    return "### Data\n\n" + table(["", "Value"], rows)


def section_resolution(r: dict) -> str:
    resolve = r["resolve"]
    if resolve is None:
        title = "### Entity resolution\n\n"
        return title + pending("python -m WikipediaLagChecker resolve")

    stats = resolve["resolver"]
    references = stats["references"]

    hit_rate = pct(stats["cache_hit_rate"])
    hits = num(stats["cache_hits"])
    negative = num(stats["negative_cache_hits"])
    p50 = num(stats["lookup_latency_p50_ms"])
    p95 = num(stats["lookup_latency_p95_ms"])
    coalesced_share = pct(_share(stats["coalesced"], references))

    with_item = (
        f"{pct(resolve['edits_with_item_rate'])} of {num(resolve['events']['kept'])}"
    )
    cache_line = (
        f"**{hit_rate}** ({hits} of {num(references)} lookups;"
        f" {negative} were negative hits)"
    )
    requests_line = (
        f"{num(stats['network_requests'])} batched requests"
        f" for {num(stats['network_titles'])} titles"
    )
    problems_line = (
        f"{num(stats['rate_limited'])} / {num(stats['retries'])}"
        f" / {num(stats['failed_titles'])} / {num(stats['api_errors'])}"
    )

    rows = [
        ["Edits resolved to a Wikidata item", with_item],
        ["**LRU cache hit rate**", cache_line],
        [
            "Coalesced within a block (repeat of a pending key)",
            f"{coalesced_share} ({num(stats['coalesced'])})",
        ],
        ["Served from the snapshot", num(stats["store_hits"])],
        ["Wikidata requests in this run", requests_line],
        ["Request latency p50 / p95", f"{p50} ms / {p95} ms"],
        ["Rate limited / retries / failed titles / API errors", problems_line],
        [
            "Cache capacity / evictions",
            f"{num(resolve['cache_size'])} / {num(stats['evictions'])}",
        ],
    ]

    note = (
        "\n\nHit rate counts only the in-memory LRU. Keys first seen in the same"
        " block are merged into one lookup and counted separately as coalesced."
    )
    return "### Entity resolution\n\n" + table(["", "Value"], rows) + note


def _metric_cell(method: dict | None, key: str, awaiting: str) -> str:
    """A precision/recall cell, or a note saying it still needs labels."""
    if method is None or method.get(key) is None:
        return awaiting
    return pct(method[key])


def section_baseline(r: dict) -> str:
    pairs = r["pairs"]
    baseline = r["baseline"]
    title = "### Baseline: title matching vs Wikidata\n\n"

    if pairs is None:
        return title + pending("python -m WikipediaLagChecker pairs sample")

    sizes = pairs["stratum_sizes"]

    naive_scores = None
    wikidata_scores = None
    labelled = 0
    if baseline is not None:
        naive_scores = baseline["naive"]
        wikidata_scores = baseline["wikidata"]
        labelled = baseline["labelled"]

    awaiting = pending("python -m WikipediaLagChecker pairs evaluate") + " (needs labels)"

    rows = [
        [
            "Cross-edition pairs found",
            num(pairs["naive_pairs"]),
            num(pairs["wikidata_pairs"]),
        ],
        [
            "Precision",
            _metric_cell(naive_scores, "precision", awaiting),
            _metric_cell(wikidata_scores, "precision", awaiting),
        ],
        [
            "Pooled recall (upper bound)",
            _metric_cell(naive_scores, "pooled_recall", awaiting),
            _metric_cell(wikidata_scores, "pooled_recall", awaiting),
        ],
    ]

    wikidata_only = num(sizes["wikidata_only"])
    if baseline is not None and baseline.get("true_pairs_missed_by_naive") is not None:
        share = pct(baseline["share_of_true_pairs_missed_by_naive"])
        missed = (
            f"{num(baseline['true_pairs_missed_by_naive'])} estimated true pairs"
            f" ({share} of all true pairs found)"
        )
    else:
        missed = (
            f"{wikidata_only} pairs are found only by Wikidata. How many are real"
            " won't be known until the sample is labelled"
        )

    allocation = pairs["allocation"]
    split_parts = []
    sampled_total = 0
    for stratum in ("both", "naive_only", "wikidata_only"):
        split_parts.append(str(allocation[stratum]))
    for count in allocation.values():
        sampled_total += count
    split = " / ".join(split_parts)

    with_item = pct(_share(pairs["pages_with_item"], pairs["pages"]))

    overview = (
        f"Over {num(pairs['pages'])} edited pages ({with_item} with an item)."
        f" Found by both: {num(sizes['both'])}; title match only:"
        f" {num(sizes['naive_only'])}; Wikidata only: {wikidata_only}."
    )
    labels_line = (
        f"Labels: {num(labelled)} of {num(sampled_total)} sampled pairs"
        f" (stratified {split}, seed {pairs['seed']}),"
        " blind sheet in `labels/pairs.csv`."
    )

    body = [
        table(["", "Title match (naive)", "Wikidata sitelinks"], rows),
        "",
        overview,
        "",
        f"**True pairs title matching misses:** {missed}.",
        "",
        labels_line,
    ]
    return title + "\n".join(body)


def _ordinal_suffix(rank: str) -> str:
    """Suffix for the ranks the join emits: 2nd, 3rd, 5th."""
    if rank == "2":
        return "nd"
    if rank == "3":
        return "rd"
    return "th"


def section_propagation(r: dict) -> str:
    prop = r["propagation"]
    title = "### Propagation\n\n"

    if prop is None:
        return title + pending("python -m WikipediaLagChecker join")

    join = prop["join"]
    rates = prop["rates"]
    lags = prop["lags"]
    watermark = prop["watermark_seconds"]
    warmup = prop["warmup_seconds"]
    closed = join["closed_with_human_edit"]

    context = (
        f"Watermark {duration(watermark)}, warm-up {duration(warmup)}, reorder buffer"
        f" {duration(prop['reorder_seconds'])}."
        f" Windows opened: {num(join['windows_opened'])};"
        f" excluded by warm-up: {num(join['warmup_windows'])};"
        f" still open at the end: {num(join['windows_open_at_end'])}."
    )

    # Nothing closed yet means there's nothing honest to report.
    if closed == 0:
        message = (
            "No propagation numbers yet. Every window either opened during warm-up or"
            " was still open when the archive ended. The first results need more than"
            f" {duration(watermark + warmup)} of continuous archive."
        )
        return title + message + "\n\n" + context

    # Headline: how many items reached each rank, and how long it took.
    rank_rows = []
    for rank, summary in lags["lag_by_rank"].items():
        rank_rows.append(
            [
                f"{rank}{_ordinal_suffix(rank)} edition",
                pct(rates["reached_rank"].get(rank)),
                num(summary["n"]),
                duration(summary["median_seconds"]),
                duration(summary["p95_seconds"]),
            ]
        )
    headline = table(
        ["Reached", "Share of closed items", "Records", "Median lag", "p95 lag"],
        rank_rows,
    )

    peak_state_parts = []
    for key in ("peak_open_windows", "peak_closed_remembered", "peak_reorder_buffer"):
        peak_state_parts.append(num(join[key]))
    peak_state = " / ".join(peak_state_parts)

    late_followups = (
        f"{pct(rates['late_followup_items'])} ({num(join['late_followup_items'])})"
    )

    facts = table(
        ["", "Value"],
        [
            ["Closed item windows (after warm-up)", num(closed)],
            ["Never reached a second edition", pct(rates["never_propagated"])],
            ["Items with a new edition after the watermark closed", late_followups],
            [
                "Bot share of cross-edition edits",
                pct(rates["bot_share_of_cross_edition_edits"]),
            ],
            ["Edits with no Wikidata item", pct(rates["edits_without_item"])],
            ["Edits later than the reorder buffer", num(join["out_of_order"])],
            ["Peak state: open windows / remembered closed / reorder buffer", peak_state],
        ],
    )

    # Lag for the most common language pairs.
    pair_rows = []
    for pair in lags["lag_by_pair"][:TOP_PAIRS]:
        pair_rows.append(
            [
                f"{pair['leader']} → {pair['follower']}",
                num(pair["n"]),
                duration(pair["median_seconds"]),
                duration(pair["p95_seconds"]),
            ]
        )
    if len(pair_rows) > 0:
        pairs_table = table(
            ["Leader → follower", "Records", "Median lag", "p95 lag"], pair_rows
        )
    else:
        pairs_table = "_No language pair has enough records yet._"

    # Which editions lead and which follow.
    edition_rows = []
    rank_number = 0
    for edition in lags["editions"]:
        rank_number += 1
        edition_rows.append(
            [
                str(rank_number),
                edition["wiki"],
                num(edition["windows"]),
                pct(edition["lead_rate"]),
                pct(edition["expected_lead_rate"]),
                f"{edition['lead_lift']:.2f}",
                duration(edition["median_follow_lag_seconds"]),
            ]
        )
    if len(edition_rows) > 0:
        editions_table = table(
            [
                "Rank",
                "Edition",
                "Windows",
                "Led",
                "Expected",
                "Lift",
                "Median lag when following",
            ],
            edition_rows,
        )
    else:
        editions_table = "_No edition has enough windows yet._"

    thin_pairs = num(lags["pairs_below_min_samples"])
    lift_note = (
        "Lift is lead rate over the rate expected if the leader of each window"
        " were drawn uniformly from its editions. Above 1 leads more than chance."
    )

    parts = [
        title + headline,
        facts,
        context,
        f"#### Lag by language pair (top {TOP_PAIRS} by records)",
        pairs_table,
        f"Pairs with fewer records than the minimum: {thin_pairs}.",
        "#### Leading and lagging editions",
        editions_table,
        lift_note,
    ]
    return "\n\n".join(parts)


def _rows_for_stage(result: dict, stage: str) -> list[dict]:
    rows = []
    for row in result["rows"]:
        if row["stage"] == stage:
            rows.append(row)
    return rows


def _row_for_workers(rows: list[dict], workers: int) -> dict:
    for row in rows:
        if row["workers"] == workers:
            return row
    raise ValueError(f"no benchmark row for {workers} workers")


def section_performance(r: dict) -> str:
    title = "### Performance\n\n"
    after = r["bench_after"]
    before = r["bench_before"]

    if after is None:
        return title + pending("python -m WikipediaLagChecker bench --label after")

    # Scaling table: full pipeline rows first, then parse-only rows.
    scaling_rows = []
    for stage in ("full", "parse"):
        for row in _rows_for_stage(after, stage):
            if row["stage"] == "full":
                stage_name = "full pipeline"
            else:
                stage_name = "parse only"
            scaling_rows.append(
                [
                    stage_name,
                    str(row["workers"]),
                    num(row["events_per_second"]),
                    f"{row['speedup']:.2f}×",
                    pct(row["efficiency"], 0),
                    f"{num(row['peak_rss_mb'])} MB",
                ]
            )
    scaling = table(
        ["Stage", "Workers", "Events/s", "Speed-up", "Efficiency", "Peak RSS"],
        scaling_rows,
    )

    peak = 0
    for row in after["rows"]:
        if row["peak_rss_mb"] > peak:
            peak = row["peak_rss_mb"]

    cores = f"{after['physical_cores']} physical / {after['cpu_count']} logical cores"
    if after["deterministic_across_workers"]:
        identical = "yes"
    else:
        identical = "**no**"

    intro = (
        f"Replay of {len(after['partitions'])} partitions, median of {after['repeats']}"
        f" runs, on {cores}. Output identical at every worker count: {identical}."
        f" Peak RSS under replay: **{num(peak)} MB** (main process plus workers)."
    )
    parts = [intro, "", scaling]

    if before is not None:
        full_after = _rows_for_stage(after, "full")
        full_before = _rows_for_stage(before, "full")

        worker_counts = set()
        for row in full_after:
            worker_counts.add(row["workers"])

        comparison = []
        for workers in sorted(worker_counts):
            b = _row_for_workers(full_before, workers)
            a = _row_for_workers(full_after, workers)
            gain = a["events_per_second"] / b["events_per_second"]
            comparison.append(
                [
                    str(workers),
                    num(b["events_per_second"]),
                    num(a["events_per_second"]),
                    f"{gain:.2f}×",
                    f"{b['speedup']:.2f}× → {a['speedup']:.2f}×",
                ]
            )

        comparison_table = table(
            [
                "Workers",
                "Before (events/s)",
                "After (events/s)",
                "Gain",
                "Scaling vs 1 worker",
            ],
            comparison,
        )
        parts.append("")
        parts.append("#### Profiled bottleneck: before and after")
        parts.append("")
        parts.append(comparison_table)

        profile_before = r["profile_before"]
        profile_after = r["profile_after"]
        if profile_before and profile_after:
            top_before = profile_before["top"][0]
            top_after = profile_after["top"][0]
            profile_line = (
                f"Profile before: `{top_before['function']}` took"
                f" {pct(top_before['own_share'])} of a"
                f" {duration(profile_before['elapsed_seconds'])}"
                f" profiled replay ({num(top_before['calls'])} calls). After: the top"
                f" entry is `{top_after['function']}` at {pct(top_after['own_share'])},"
                f" and the profiled replay takes"
                f" {duration(profile_after['elapsed_seconds'])}."
            )
            parts.append("")
            parts.append(profile_line)

    return title + "\n".join(parts)


def section_failures(r: dict) -> str:
    title = "### Failure analysis\n\n"
    confirmed = r["failures"]
    sample = r["failures_sample"]

    # Reviewed cases exist: show them by cause.
    if confirmed is not None and confirmed["confirmed_wrong"]:
        rows = []
        for category in confirmed["categories"]:
            rows.append(
                [
                    category["category"],
                    num(category["count"]),
                    ", ".join(category["cases"]),
                ]
            )
        summary = (
            f"{num(confirmed['confirmed_wrong'])} confirmed wrong of"
            f" {num(confirmed['reviewed'])} reviewed. The suggested category matched the"
            f" reviewer's in {pct(confirmed['heuristic_agreement'])} of confirmed cases."
        )
        cause_table = table(["Cause", "Cases", "Case ids (labels/failures.csv)"], rows)
        return title + cause_table + "\n\n" + summary

    # A sheet was written but nobody has reviewed it yet.
    if sample is not None:
        return (
            title
            + pending("python -m WikipediaLagChecker failures summarise")
            + f" (needs review). {num(sample['candidates'])} candidates are in"
            " `labels/failures.csv`."
        )

    return title + pending("python -m WikipediaLagChecker failures sample")


def render(results: dict[str, dict | None]) -> str:
    sections = [
        section_data(results),
        section_resolution(results),
        section_baseline(results),
        section_propagation(results),
        section_performance(results),
        section_failures(results),
    ]

    # The footer lists the run id behind each result, so tables can be traced.
    source_parts = []
    for name, result in results.items():
        if result is not None and "run_id" in result:
            source_parts.append(f"`{name}` ({result['run_id']})")
    sources = ", ".join(source_parts)
    if sources == "":
        sources = "none"

    footer = "<sub>Generated by `python -m WikipediaLagChecker report`"
    footer += " from `results/*.json`. Runs: "
    footer += sources + ".</sub>"

    sections.append(footer)
    return "\n\n".join(sections)


def update_readme(readme: Path, rendered: str) -> bool:
    """Replace the block between the markers. Returns True if anything changed."""
    text = readme.read_text(encoding="utf-8")
    start = text.find(MARK_START)
    end = text.find(MARK_END)
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"{readme} is missing the results markers")

    before_block = text[: start + len(MARK_START)]
    after_block = text[end:]
    updated = before_block + "\n\n" + rendered + "\n\n" + after_block

    if updated == text:
        return False
    readme.write_text(updated, encoding="utf-8")
    return True
