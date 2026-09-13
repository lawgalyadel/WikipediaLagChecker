"""Render the README results section from `results/*.json`.

Every number in the README's results block comes from here, and every
number here comes from a results file written by a run. A result that has
not been produced renders as the command that would produce it, never as
a placeholder value. The block between the markers is replaced wholesale,
so hand edits inside it are overwritten by design.
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
    loaded: dict[str, dict | None] = {}
    for name in RESULT_NAMES:
        path = directory / f"{name}.json"
        loaded[name] = (
            json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        )
    return loaded


# --- formatting --------------------------------------------------------------


def num(value: float | int | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.1f}"
    return f"{int(value):,}"


def pct(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = round(seconds)
    if seconds < 90:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 90:
        return f"{minutes}m {rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def plural(count: int, noun: str) -> str:
    return f"{num(count)} {noun}{'' if count == 1 else 's'}"


def pending(command: str) -> str:
    return f"_not measured yet — run `{command}`_"


def table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def _share(part: int | None, whole: int | None) -> float | None:
    return part / whole if part is not None and whole else None


# --- sections ----------------------------------------------------------------


def section_data(r: dict) -> str:
    archive, resolve = r["archive"], r["resolve"]
    if archive is None:
        return "### Data\n\n" + pending("wikilag stats")
    gaps = sorted(archive["gaps"], key=lambda gap: -gap[2])
    largest = ", ".join(
        f"{g[2]} min from {g[0][11:16]} UTC {g[0][:10]}" for g in gaps[:3]
    )
    rows = [
        [
            "Archive window (event time, UTC)",
            f"{archive['first_event'][:16]} → {archive['last_event'][:16]}"
            f" ({archive['partitions']} hourly partitions)",
        ],
        ["Raw events archived", num(archive["events"])],
        [
            "Undecodable archive lines",
            f"{num(archive['undecodable'])} ({pct(archive['undecodable_rate'], 4)})",
        ],
        ["Damaged gzip members recovered", num(archive["damaged_members"])],
        [
            "Coverage gaps",
            f"{plural(len(gaps), 'gap')}, {plural(archive['gap_minutes'], 'minute')}"
            + (f" — largest: {largest}" if gaps else ""),
        ],
    ]
    if resolve is not None:
        events = resolve["events"]
        rows += [
            ["Article edits after filtering", num(events["kept"])],
            ["Duplicates removed (restart re-archiving)", num(events["duplicate"])],
            [
                "Filtered out",
                ", ".join(f"{k} {num(v)}" for k, v in sorted(events["filtered"].items()))
                or "0",
            ],
        ]
    return "### Data\n\n" + table(["", "Value"], rows)


def section_resolution(r: dict) -> str:
    resolve = r["resolve"]
    if resolve is None:
        return "### Entity resolution\n\n" + pending("wikilag resolve")
    s = resolve["resolver"]
    refs = s["references"]
    hit_rate, hits, negative = (
        pct(s["cache_hit_rate"]),
        num(s["cache_hits"]),
        num(s["negative_cache_hits"]),
    )
    p50, p95 = num(s["lookup_latency_p50_ms"]), num(s["lookup_latency_p95_ms"])
    rows = [
        [
            "Edits resolved to a Wikidata item",
            f"{pct(resolve['edits_with_item_rate'])} of {num(resolve['events']['kept'])}",
        ],
        [
            "**LRU cache hit rate**",
            f"**{hit_rate}** ({hits} of {num(refs)} lookups;"
            f" {negative} were negative hits)",
        ],
        [
            "Coalesced within a block (repeat of a pending key)",
            f"{pct(_share(s['coalesced'], refs))} ({num(s['coalesced'])})",
        ],
        ["Served from the snapshot", num(s["store_hits"])],
        [
            "Wikidata requests in this run",
            f"{num(s['network_requests'])} batched requests"
            f" for {num(s['network_titles'])} titles",
        ],
        ["Request latency p50 / p95", f"{p50} ms / {p95} ms"],
        [
            "Rate limited / retries / failed titles / API errors",
            f"{num(s['rate_limited'])} / {num(s['retries'])} / {num(s['failed_titles'])}"
            f" / {num(s['api_errors'])}",
        ],
        [
            "Cache capacity / evictions",
            f"{num(resolve['cache_size'])} / {num(s['evictions'])}",
        ],
    ]
    note = (
        "\n\nHit rate counts only the in-memory LRU. Keys first seen in the same"
        " block are merged into one lookup and counted separately as coalesced."
    )
    return "### Entity resolution\n\n" + table(["", "Value"], rows) + note


def section_baseline(r: dict) -> str:
    pairs, baseline = r["pairs"], r["baseline"]
    if pairs is None:
        return "### Baseline: title matching vs Wikidata\n\n" + pending(
            "wikilag pairs sample"
        )
    sizes = pairs["stratum_sizes"]
    naive_m = baseline["naive"] if baseline else None
    wd_m = baseline["wikidata"] if baseline else None
    labelled = baseline["labelled"] if baseline else 0
    awaiting = pending("wikilag pairs evaluate") + " (needs labels)"

    def metric(m: dict | None, key: str) -> str:
        return awaiting if m is None or m.get(key) is None else pct(m[key])

    rows = [
        [
            "Cross-edition pairs found",
            num(pairs["naive_pairs"]),
            num(pairs["wikidata_pairs"]),
        ],
        ["Precision", metric(naive_m, "precision"), metric(wd_m, "precision")],
        [
            "Pooled recall (upper bound)",
            metric(naive_m, "pooled_recall"),
            metric(wd_m, "pooled_recall"),
        ],
    ]
    wikidata_only = num(sizes["wikidata_only"])
    if baseline and baseline.get("true_pairs_missed_by_naive") is not None:
        share = pct(baseline["share_of_true_pairs_missed_by_naive"])
        missed = (
            f"{num(baseline['true_pairs_missed_by_naive'])} estimated true pairs"
            f" ({share} of all true pairs found)"
        )
    else:
        missed = f"{wikidata_only} pairs are found only by Wikidata; how many are true"
        missed += " awaits the labels"
    allocation = pairs["allocation"]
    split = " / ".join(
        str(allocation[s]) for s in ("both", "naive_only", "wikidata_only")
    )
    with_item = pct(_share(pairs["pages_with_item"], pairs["pages"]))
    body = [
        table(["", "Title match (naive)", "Wikidata sitelinks"], [*rows]),
        "",
        f"Over {num(pairs['pages'])} edited pages ({with_item} with an item)."
        f" Found by both: {num(sizes['both'])}; title match only:"
        f" {num(sizes['naive_only'])}; Wikidata only: {wikidata_only}.",
        "",
        f"**True pairs title matching misses:** {missed}.",
        "",
        f"Labels: {num(labelled)} of {num(sum(allocation.values()))} sampled pairs"
        f" (stratified {split}, seed {pairs['seed']}),"
        " blind sheet in `labels/pairs.csv`.",
    ]
    return "### Baseline: title matching vs Wikidata\n\n" + "\n".join(body)


def section_propagation(r: dict) -> str:
    prop = r["propagation"]
    title = "### Propagation\n\n"
    if prop is None:
        return title + pending("wikilag join")
    join, rates, lags = prop["join"], prop["rates"], prop["lags"]
    watermark, warmup = prop["watermark_seconds"], prop["warmup_seconds"]
    closed = join["closed_with_human_edit"]
    reorder, opened = duration(prop["reorder_seconds"]), num(join["windows_opened"])
    context = (
        f"Watermark {duration(watermark)}, warm-up {duration(warmup)}, reorder buffer"
        f" {reorder}. Windows opened: {opened};"
        f" excluded by warm-up: {num(join['warmup_windows'])}; still open at the end:"
        f" {num(join['windows_open_at_end'])}."
    )
    if closed == 0:
        return (
            title
            + "**No propagation numbers yet.** Every item window either opened inside the"
            f" warm-up or had not closed when the archive ended, so no lag, rank or"
            f" late-follow-up figure can be stated. A first result needs more than"
            f" {duration(watermark + warmup)} of continuous archive.\n\n" + context
        )

    rank_rows = []
    for rank, summary in lags["lag_by_rank"].items():
        rank_rows.append(
            [
                f"{rank}{'nd' if rank == '2' else 'rd' if rank == '3' else 'th'} edition",
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
    facts = table(
        ["", "Value"],
        [
            ["Closed item windows (after warm-up)", num(closed)],
            ["Never reached a second edition", pct(rates["never_propagated"])],
            [
                "Items with a new edition after the watermark closed",
                f"{pct(rates['late_followup_items'])}"
                f" ({num(join['late_followup_items'])})",
            ],
            [
                "Bot share of cross-edition edits",
                pct(rates["bot_share_of_cross_edition_edits"]),
            ],
            ["Edits with no Wikidata item", pct(rates["edits_without_item"])],
            ["Edits later than the reorder buffer", num(join["out_of_order"])],
            [
                "Peak state: open windows / remembered closed / reorder buffer",
                " / ".join(
                    num(join[key])
                    for key in (
                        "peak_open_windows",
                        "peak_closed_remembered",
                        "peak_reorder_buffer",
                    )
                ),
            ],
        ],
    )
    pair_rows = [
        [
            f"{p['leader']} → {p['follower']}",
            num(p["n"]),
            duration(p["median_seconds"]),
            duration(p["p95_seconds"]),
        ]
        for p in lags["lag_by_pair"][:TOP_PAIRS]
    ]
    pairs_table = (
        table(["Leader → follower", "Records", "Median lag", "p95 lag"], pair_rows)
        if pair_rows
        else "_No language pair has enough records yet._"
    )
    edition_rows = [
        [
            str(index),
            e["wiki"],
            num(e["windows"]),
            pct(e["lead_rate"]),
            pct(e["expected_lead_rate"]),
            f"{e['lead_lift']:.2f}",
            duration(e["median_follow_lag_seconds"]),
        ]
        for index, e in enumerate(lags["editions"], 1)
    ]
    editions_table = (
        table(
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
        if edition_rows
        else "_No edition has enough windows yet._"
    )
    thin_pairs = num(lags["pairs_below_min_samples"])
    return (
        title
        + headline
        + "\n\n"
        + facts
        + "\n\n"
        + context
        + f"\n\n#### Lag by language pair (top {TOP_PAIRS} by records)\n\n"
        + pairs_table
        + f"\n\nPairs with fewer records than the minimum: {thin_pairs}."
        + "\n\n#### Leading and lagging editions\n\n"
        + editions_table
        + "\n\nLift is lead rate over the rate expected if the leader of each window"
        " were drawn uniformly from its editions. Above 1 leads more than chance."
    )


def section_performance(r: dict) -> str:
    title = "### Performance\n\n"
    after, before = r["bench_after"], r["bench_before"]
    if after is None:
        return title + pending("wikilag bench --label after")

    def rows_for(result: dict, stage: str) -> list[dict]:
        return [row for row in result["rows"] if row["stage"] == stage]

    scaling = table(
        ["Stage", "Workers", "Events/s", "Speed-up", "Efficiency", "Peak RSS"],
        [
            [
                "full pipeline" if row["stage"] == "full" else "parse only",
                str(row["workers"]),
                num(row["events_per_second"]),
                f"{row['speedup']:.2f}×",
                pct(row["efficiency"], 0),
                f"{num(row['peak_rss_mb'])} MB",
            ]
            for stage in ("full", "parse")
            for row in rows_for(after, stage)
        ],
    )
    peak = max(row["peak_rss_mb"] for row in after["rows"])
    cores = f"{after['physical_cores']} physical / {after['cpu_count']} logical cores"
    identical = "yes" if after["deterministic_across_workers"] else "**no**"
    parts = [
        f"Replay of {len(after['partitions'])} partitions, median of {after['repeats']}"
        f" runs, on {cores}. Output identical at every worker count: {identical}."
        f" Peak RSS under replay: **{num(peak)} MB** (main process plus workers).",
        "",
        scaling,
    ]
    if before is not None:
        comparison = []
        for workers in sorted({row["workers"] for row in rows_for(after, "full")}):
            b = next(x for x in rows_for(before, "full") if x["workers"] == workers)
            a = next(x for x in rows_for(after, "full") if x["workers"] == workers)
            comparison.append(
                [
                    str(workers),
                    num(b["events_per_second"]),
                    num(a["events_per_second"]),
                    f"{a['events_per_second'] / b['events_per_second']:.2f}×",
                    f"{b['speedup']:.2f}× → {a['speedup']:.2f}×",
                ]
            )
        parts += [
            "",
            "#### Profiled bottleneck: before and after",
            "",
            table(
                [
                    "Workers",
                    "Before (events/s)",
                    "After (events/s)",
                    "Gain",
                    "Scaling vs 1 worker",
                ],
                comparison,
            ),
        ]
        pb, pa = r["profile_before"], r["profile_after"]
        if pb and pa:
            top_before, top_after = pb["top"][0], pa["top"][0]
            parts += [
                "",
                f"Profile before: `{top_before['function']}` took"
                f" {pct(top_before['own_share'])} of a {duration(pb['elapsed_seconds'])}"
                f" profiled replay ({num(top_before['calls'])} calls). After: the top"
                f" entry is `{top_after['function']}` at {pct(top_after['own_share'])},"
                f" and the profiled replay takes {duration(pa['elapsed_seconds'])}.",
            ]
    return title + "\n".join(parts)


def section_failures(r: dict) -> str:
    title = "### Failure analysis\n\n"
    confirmed, sample = r["failures"], r["failures_sample"]
    if confirmed is not None and confirmed["confirmed_wrong"]:
        rows = [
            [c["category"], num(c["count"]), ", ".join(c["cases"])]
            for c in confirmed["categories"]
        ]
        return (
            title
            + table(["Cause", "Cases", "Case ids (labels/failures.csv)"], rows)
            + f"\n\n{num(confirmed['confirmed_wrong'])} confirmed wrong of"
            f" {num(confirmed['reviewed'])} reviewed. The suggested category matched the"
            f" reviewer's in {pct(confirmed['heuristic_agreement'])} of confirmed cases."
        )
    if sample is not None:
        return (
            title
            + pending("wikilag failures summarise")
            + f" (needs review). {num(sample['candidates'])} candidates are in"
            " `labels/failures.csv`."
        )
    return title + pending("wikilag failures sample")


def render(results: dict[str, dict | None]) -> str:
    sections = [
        section_data(results),
        section_resolution(results),
        section_baseline(results),
        section_propagation(results),
        section_performance(results),
        section_failures(results),
    ]
    sources = ", ".join(
        f"`{name}` ({result['run_id']})"
        for name, result in results.items()
        if result is not None and "run_id" in result
    )
    footer = (
        "<sub>Generated by `wikilag report` from `results/*.json`. Runs: "
        + (sources or "none")
        + ".</sub>"
    )
    return "\n\n".join([*sections, footer])


def update_readme(readme: Path, rendered: str) -> bool:
    text = readme.read_text(encoding="utf-8")
    start, end = text.find(MARK_START), text.find(MARK_END)
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"{readme} is missing the results markers")
    updated = text[: start + len(MARK_START)] + "\n\n" + rendered + "\n\n" + text[end:]
    if updated == text:
        return False
    readme.write_text(updated, encoding="utf-8")
    return True
