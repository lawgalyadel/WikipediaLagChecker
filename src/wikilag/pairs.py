"""Cross-edition page pairs: naive title matching vs Wikidata sitelinks.

Both methods run over the same universe: every page with at least one
human edit in the analysed partitions. A pair is two pages on different
editions that a method claims are the same subject.

Validation uses 200 hand labels, drawn by stratified sampling from the
pooled pairs of both methods (found by both / naive only / Wikidata only),
with estimates reweighted by stratum size. A uniform sample would be almost
entirely "both", leaving the disagreements — where the methods actually
differ — too thin to measure.

The labelling sheet is blind: shuffled, with no stratum, method or item id
on it. The key linking rows to strata is written to a separate file.

Recall is pooled recall: relative to true pairs found by at least one
method. A true pair neither method finds is invisible here, so both recall
figures are upper bounds. This is stated with the results.
"""

from __future__ import annotations

import csv
import random
from collections import defaultdict
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
        return {
            "both": sorted(self.naive & self.wikidata),
            "naive_only": sorted(self.naive - self.wikidata),
            "wikidata_only": sorted(self.wikidata - self.naive),
        }


def _pairs_within(groups: Iterable[list[Page]]) -> set[Pair]:
    pairs: set[Pair] = set()
    for pages in groups:
        ordered = sorted(pages)
        for i, first in enumerate(ordered):
            for second in ordered[i + 1 :]:
                if first[0] != second[0]:
                    pairs.add((first, second))
    return pairs


def build_pairs(resolved: Iterable[tuple[Edit, str | None]]) -> PairSets:
    """Pairs from both methods over pages with a human edit."""
    items: dict[Page, str | None] = {}
    for edit, qid in resolved:
        if not edit.bot:
            items[edit.key] = qid

    by_title: dict[str, list[Page]] = defaultdict(list)
    by_item: dict[str, list[Page]] = defaultdict(list)
    for page, qid in items.items():
        by_title[page[1]].append(page)
        if qid is not None:
            by_item[qid].append(page)

    return PairSets(
        pages=len(items),
        pages_with_item=sum(qid is not None for qid in items.values()),
        naive=frozenset(_pairs_within(by_title.values())),
        wikidata=frozenset(_pairs_within(by_item.values())),
    )


def allocate(sizes: dict[str, int], total: int) -> dict[str, int]:
    """Split `total` evenly across strata, giving a small stratum's unused
    share to the others. Smallest strata are capped first, so what they
    cannot use is spread evenly over the rest. Deterministic."""
    allocation = dict.fromkeys(sizes, 0)
    remaining = total
    pending = sorted(
        (s for s in STRATA if sizes.get(s, 0) > 0),
        key=lambda s: (sizes[s], STRATA.index(s)),
    )
    for index, stratum in enumerate(pending):
        take = min(sizes[stratum], remaining // (len(pending) - index))
        allocation[stratum] = take
        remaining -= take
    return allocation


def page_url(page: Page) -> str:
    wiki, title = page
    language = wiki.removesuffix("wiki").replace("_", "-")
    return f"https://{language}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"


def sample_for_labelling(
    pair_sets: PairSets, size: int, seed: int
) -> tuple[list[dict], list[dict], dict[str, int]]:
    """Blind labelling rows, the stratum key, and the allocation used."""
    strata = pair_sets.strata()
    allocation = allocate({s: len(p) for s, p in strata.items()}, size)
    rng = random.Random(seed)

    chosen: list[tuple[str, Pair]] = []
    for stratum in STRATA:
        chosen += [
            (stratum, pair) for pair in rng.sample(strata[stratum], allocation[stratum])
        ]
    rng.shuffle(chosen)

    rows, key = [], []
    for index, (stratum, (page_a, page_b)) in enumerate(chosen, 1):
        pair_id = f"p{index:03d}"
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
    with key_path(labels_path).open(encoding="utf-8", newline="") as handle:
        strata_of = {row["pair_id"]: row["stratum"] for row in csv.DictReader(handle)}
    with labels_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    labelled = dict.fromkeys(STRATA, 0)
    true = dict.fromkeys(STRATA, 0)
    unlabelled = 0
    for row in rows:
        label = parse_label(row.get("same_subject", ""))
        if label is None:
            unlabelled += 1
            continue
        stratum = strata_of[row["pair_id"]]
        labelled[stratum] += 1
        true[stratum] += label

    # Estimated true pairs in each full stratum.
    estimated_true = {
        s: (true[s] / labelled[s]) * stratum_sizes[s] if labelled[s] else None
        for s in STRATA
    }

    def method(strata: tuple[str, str]) -> dict:
        if any(estimated_true[s] is None for s in (*strata, *STRATA)):
            return {"precision": None, "pooled_recall": None, "pairs": None}
        found = sum(stratum_sizes[s] for s in strata)
        true_found = sum(estimated_true[s] for s in strata)
        pooled_true = sum(estimated_true[s] for s in STRATA)
        return {
            "pairs": found,
            "estimated_true_pairs": round(true_found, 1),
            "precision": round(true_found / found, 4) if found else None,
            "pooled_recall": round(true_found / pooled_true, 4) if pooled_true else None,
        }

    missed_by_naive = estimated_true["wikidata_only"]
    pooled_true_total = (
        sum(estimated_true.values()) if None not in estimated_true.values() else None
    )
    return {
        "labelled": sum(labelled.values()),
        "unlabelled": unlabelled,
        "labelled_by_stratum": labelled,
        "true_by_stratum": true,
        "stratum_sizes": stratum_sizes,
        "estimated_true_by_stratum": {
            s: None if v is None else round(v, 1) for s, v in estimated_true.items()
        },
        "naive": method(("both", "naive_only")),
        "wikidata": method(("both", "wikidata_only")),
        "true_pairs_missed_by_naive": None
        if missed_by_naive is None
        else round(missed_by_naive, 1),
        "share_of_true_pairs_missed_by_naive": None
        if pooled_true_total in (None, 0)
        else round(missed_by_naive / pooled_true_total, 4),
    }
