"""Cross-edition page pairs: naive title matching vs Wikidata sitelinks.

Both methods run over the same universe: every page with at least one
human edit in the analysed partitions. A pair is two pages on different
editions that a method claims are the same subject.

Validation uses 200 hand labels, drawn by stratified sampling from the
pooled pairs of both methods (found by both / naive only / Wikidata only),
with estimates reweighted by stratum size. A uniform sample would be almost
all "both", with too few of the pairs where the methods disagree.

The labelling sheet is blind: shuffled, with no stratum, method or item id
on it. The key linking rows to strata is written to a separate file.

Recall is pooled recall: relative to true pairs found by at least one
method. A true pair neither method finds is invisible here, so both recall
figures are upper bounds.
"""

from __future__ import annotations

import csv
import random
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from wikilag.events import Edit

Page = tuple[str, str]  # (wiki, title)
Pair = tuple[Page, Page]  # sorted, so each unordered pair has one spelling

STRATA = ("both", "naive_only", "wikidata_only")
LABEL_FIELDS = [
    "pair_id",
    "wiki_a",
    "title_a",
    "url_a",
    "wiki_b",
    "title_b",
    "url_b",
    "same_subject",
    "note",
]
TRUE_LABELS = {"y", "yes", "1", "true", "same"}
FALSE_LABELS = {"n", "no", "0", "false", "different"}


@dataclass(frozen=True)
class PairSets:
    pages: int
    pages_with_item: int
    naive: frozenset[Pair]
    wikidata: frozenset[Pair]

    def strata(self) -> dict[str, list[Pair]]:
        found_by_both = self.naive & self.wikidata
        naive_only = self.naive - self.wikidata
        wikidata_only = self.wikidata - self.naive

        return {
            "both": sorted(found_by_both),
            "naive_only": sorted(naive_only),
            "wikidata_only": sorted(wikidata_only),
        }


def _pairs_within(groups: Iterable[list[Page]]) -> set[Pair]:
    """Every pair of pages from different wikis inside each group."""
    pairs: set[Pair] = set()

    for pages in groups:
        ordered = sorted(pages)
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                first = ordered[i]
                second = ordered[j]
                # Two pages on the same wiki aren't a cross-edition pair.
                if first[0] != second[0]:
                    pairs.add((first, second))

    return pairs


def build_pairs(resolved: Iterable[tuple[Edit, str | None]]) -> PairSets:
    """Pairs from both methods over pages with a human edit."""
    # page -> Wikidata item (or None), for every page a human edited
    items: dict[Page, str | None] = {}
    for edit, qid in resolved:
        if not edit.bot:
            items[edit.key] = qid

    # Group pages two ways: by exact title, and by Wikidata item.
    by_title: dict[str, list[Page]] = {}
    by_item: dict[str, list[Page]] = {}
    pages_with_item = 0

    for page, qid in items.items():
        title = page[1]
        if title not in by_title:
            by_title[title] = []
        by_title[title].append(page)

        if qid is not None:
            pages_with_item += 1
            if qid not in by_item:
                by_item[qid] = []
            by_item[qid].append(page)

    naive_pairs = _pairs_within(by_title.values())
    wikidata_pairs = _pairs_within(by_item.values())

    return PairSets(
        pages=len(items),
        pages_with_item=pages_with_item,
        naive=frozenset(naive_pairs),
        wikidata=frozenset(wikidata_pairs),
    )


def allocate(sizes: dict[str, int], total: int) -> dict[str, int]:
    """Split `total` evenly across strata, giving a small stratum's unused
    share to the others. Smallest strata are capped first, so what they
    cannot use is spread evenly over the rest. Deterministic."""
    allocation = {}
    for stratum in sizes:
        allocation[stratum] = 0

    # Only non-empty strata get a share, smallest first (ties in STRATA order).
    non_empty = []
    for stratum in STRATA:
        if sizes.get(stratum, 0) > 0:
            non_empty.append(stratum)

    def smallest_first(stratum: str) -> tuple[int, int]:
        return (sizes[stratum], STRATA.index(stratum))

    non_empty.sort(key=smallest_first)

    remaining = total
    for index in range(len(non_empty)):
        stratum = non_empty[index]
        strata_left = len(non_empty) - index
        fair_share = remaining // strata_left

        # A stratum can't give more pairs than it has.
        take = fair_share
        if sizes[stratum] < take:
            take = sizes[stratum]

        allocation[stratum] = take
        remaining -= take

    return allocation


def page_url(page: Page) -> str:
    wiki = page[0]
    title = page[1]

    # "enwiki" -> "en", "zh_yuewiki" -> "zh-yue"
    language = wiki.removesuffix("wiki").replace("_", "-")
    path = quote(title.replace(" ", "_"))
    return f"https://{language}.wikipedia.org/wiki/{path}"


def sample_for_labelling(
    pair_sets: PairSets, size: int, seed: int
) -> tuple[list[dict], list[dict], dict[str, int]]:
    """Blind labelling rows, the stratum key, and the allocation used."""
    strata = pair_sets.strata()

    stratum_sizes = {}
    for stratum, pairs in strata.items():
        stratum_sizes[stratum] = len(pairs)
    allocation = allocate(stratum_sizes, size)

    rng = random.Random(seed)

    chosen: list[tuple[str, Pair]] = []
    for stratum in STRATA:
        sampled = rng.sample(strata[stratum], allocation[stratum])
        for pair in sampled:
            chosen.append((stratum, pair))

    # Shuffle so the sheet doesn't give away which stratum a row came from.
    rng.shuffle(chosen)

    rows = []
    key = []
    for index in range(len(chosen)):
        stratum, pair = chosen[index]
        page_a = pair[0]
        page_b = pair[1]
        pair_id = f"p{index + 1:03d}"

        rows.append(
            {
                "pair_id": pair_id,
                "wiki_a": page_a[0],
                "title_a": page_a[1],
                "url_a": page_url(page_a),
                "wiki_b": page_b[0],
                "title_b": page_b[1],
                "url_b": page_url(page_b),
                "same_subject": "",
                "note": "",
            }
        )
        key.append({"pair_id": pair_id, "stratum": stratum})

    return rows, key, allocation


def key_path(labels_path: Path) -> Path:
    return labels_path.with_name(labels_path.stem + ".key.csv")


def write_labelling_files(labels_path: Path, rows: list[dict], key: list[dict]) -> None:
    labels_path.parent.mkdir(parents=True, exist_ok=True)

    with labels_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LABEL_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    with key_path(labels_path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["pair_id", "stratum"])
        writer.writeheader()
        writer.writerows(key)


def parse_label(value: str) -> bool | None:
    normalised = value.strip().lower()
    if normalised in TRUE_LABELS:
        return True
    if normalised in FALSE_LABELS:
        return False
    return None


def evaluate(labels_path: Path, stratum_sizes: dict[str, int]) -> dict:
    """Precision and pooled recall for both methods from stratified labels."""
    strata_of = {}
    with key_path(labels_path).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            strata_of[row["pair_id"]] = row["stratum"]

    with labels_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    labelled = {}
    true = {}
    for stratum in STRATA:
        labelled[stratum] = 0
        true[stratum] = 0

    unlabelled = 0
    for row in rows:
        label = parse_label(row.get("same_subject", ""))
        if label is None:
            unlabelled += 1
            continue

        stratum = strata_of[row["pair_id"]]
        labelled[stratum] += 1
        if label:
            true[stratum] += 1

    # Estimated true pairs in each full stratum: the share of labelled pairs
    # that were true, scaled up to the stratum's size.
    estimated_true = {}
    for stratum in STRATA:
        if labelled[stratum] > 0:
            share_true = true[stratum] / labelled[stratum]
            estimated_true[stratum] = share_true * stratum_sizes[stratum]
        else:
            estimated_true[stratum] = None

    all_strata_estimated = True
    for stratum in STRATA:
        if estimated_true[stratum] is None:
            all_strata_estimated = False

    pooled_true_total = None
    if all_strata_estimated:
        pooled_true_total = 0
        for value in estimated_true.values():
            pooled_true_total += value

    rounded_estimates = {}
    for stratum, value in estimated_true.items():
        if value is None:
            rounded_estimates[stratum] = None
        else:
            rounded_estimates[stratum] = round(value, 1)

    missed_by_naive = estimated_true["wikidata_only"]

    true_pairs_missed_by_naive = None
    if missed_by_naive is not None:
        true_pairs_missed_by_naive = round(missed_by_naive, 1)

    share_missed_by_naive = None
    if pooled_true_total is not None and pooled_true_total != 0:
        share_missed_by_naive = round(missed_by_naive / pooled_true_total, 4)

    total_labelled = 0
    for count in labelled.values():
        total_labelled += count

    return {
        "labelled": total_labelled,
        "unlabelled": unlabelled,
        "labelled_by_stratum": labelled,
        "true_by_stratum": true,
        "stratum_sizes": stratum_sizes,
        "estimated_true_by_stratum": rounded_estimates,
        "naive": _method_scores(("both", "naive_only"), estimated_true, stratum_sizes),
        "wikidata": _method_scores(
            ("both", "wikidata_only"), estimated_true, stratum_sizes
        ),
        "true_pairs_missed_by_naive": true_pairs_missed_by_naive,
        "share_of_true_pairs_missed_by_naive": share_missed_by_naive,
    }


def _method_scores(
    method_strata: tuple[str, str],
    estimated_true: dict[str, float | None],
    stratum_sizes: dict[str, int],
) -> dict:
    """Precision and pooled recall for a method that finds `method_strata`."""
    # Every stratum needs labels, or the numbers can't be estimated.
    for stratum in STRATA:
        if estimated_true[stratum] is None:
            return {"precision": None, "pooled_recall": None, "pairs": None}

    found = 0
    true_found = 0
    for stratum in method_strata:
        found += stratum_sizes[stratum]
        true_found += estimated_true[stratum]

    pooled_true = 0
    for stratum in STRATA:
        pooled_true += estimated_true[stratum]

    precision = None
    if found:
        precision = round(true_found / found, 4)

    pooled_recall = None
    if pooled_true:
        pooled_recall = round(true_found / pooled_true, 4)

    return {
        "pairs": found,
        "estimated_true_pairs": round(true_found, 1),
        "precision": precision,
        "pooled_recall": pooled_recall,
    }
