"""Failure analysis: find and categorise propagation pairs that are wrong.

The join cannot know which of its own records are wrong, so a person
decides. This module does everything else: samples records, recovers the
evidence behind each from the archive (comment, user, size change, bot
flag, stream skew), and suggests a cause. The reviewer fills in
`confirmed_wrong` and `category`; the README table counts confirmed rows.

Suggested categories, first match wins:

- bot_slipped_filter    username looks like a bot, but `bot` was false
- revert_or_vandalism   a revert on either side: damage control, not news
- disambiguation        a disambiguation page, not a subject
- trivial_maintenance   both edits tiny: coincident upkeep, not propagation
- out_of_order          stream skew beyond the reorder buffer on either side
- same_editor            one person editing both editions (kept separate:
                        arguably real propagation, arguably not)
- needs_review          no heuristic fired
"""

from __future__ import annotations

import csv
import gzip
import random
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from WikipediaLagChecker.config import Config
from WikipediaLagChecker.pairs import parse_label

SHEET_FIELDS = [
    "case_id",
    "qid",
    "rank",
    "lag_seconds",
    "leader_wiki",
    "leader_title",
    "leader_user",
    "leader_comment",
    "leader_bytes",
    "follower_wiki",
    "follower_title",
    "follower_user",
    "follower_comment",
    "follower_bytes",
    "suggested_category",
    "evidence",
    "confirmed_wrong",
    "category",
    "note",
]


@dataclass(frozen=True)
class EditEvidence:
    user: str
    comment: str
    bytes_changed: int | None
    bot: bool
    stream_skew_seconds: float | None


def edit_key(record: dict, side: str) -> tuple[str, str, int]:
    """(wiki, title, edit time) of one side of a propagation record."""
    wiki = record[f"{side}_wiki"]
    title = record[f"{side}_title"]
    edit_time = int(record[f"{side}_timestamp"])
    return (wiki, title, edit_time)


def read_records(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader)


def sample_records(records: list[dict], size: int, seed: int) -> list[dict]:
    # Sort first so the sample doesn't depend on the order records were written.
    def by_item_and_rank(record: dict) -> tuple[str, int]:
        return (record["qid"], int(record["rank"]))

    ordered = sorted(records, key=by_item_and_rank)

    sample_size = size
    if len(ordered) < sample_size:
        sample_size = len(ordered)

    rng = random.Random(seed)
    return rng.sample(ordered, sample_size)


def evidence_from_events(
    events: Iterable[dict], wanted: set[tuple[str, str, int]]
) -> dict[tuple[str, str, int], EditEvidence]:
    """Evidence for edits keyed by (wiki, title, edit time)."""
    found: dict[tuple[str, str, int], EditEvidence] = {}

    for event in events:
        try:
            key = (event["wiki"], event["title"], int(event["timestamp"]))
        except (KeyError, TypeError, ValueError):
            continue

        if key not in wanted:
            continue
        if key in found:
            continue

        # Size change in bytes, if the event has it.
        length = event.get("length")
        if not length:
            length = {}
        old_length = length.get("old")
        new_length = length.get("new")

        bytes_changed = None
        if new_length is not None:
            if old_length:
                bytes_changed = new_length - old_length
            else:
                bytes_changed = new_length

        # How far the stream's timestamp is from the edit time.
        skew = None
        meta = event.get("meta")
        if not meta:
            meta = {}
        dt = meta.get("dt")
        if isinstance(dt, str):
            try:
                stream_time = datetime.fromisoformat(dt.replace("Z", "+00:00"))
                skew = stream_time.timestamp() - key[2]
            except ValueError:
                skew = None

        found[key] = EditEvidence(
            user=event.get("user", ""),
            comment=event.get("comment", ""),
            bytes_changed=bytes_changed,
            bot=bool(event.get("bot", False)),
            stream_skew_seconds=skew,
        )

    return found


def suggest(
    record: dict,
    leader: EditEvidence | None,
    follower: EditEvidence | None,
    config: Config,
) -> tuple[str, str]:
    """Suggested category and the evidence that triggered it."""
    rules = config.failures
    sides = [("leader", leader), ("follower", follower)]

    # 1. A user that looks like a bot but wasn't flagged as one.
    bot_pattern = re.compile(rules.bot_user_pattern)
    for side, evidence in sides:
        if evidence is None:
            continue
        if not evidence.bot and bot_pattern.search(evidence.user):
            return "bot_slipped_filter", f"{side} user {evidence.user!r}"

    # 2. A revert on either side.
    revert_patterns = []
    for pattern in rules.revert_comment_patterns:
        revert_patterns.append(re.compile(pattern))

    for side, evidence in sides:
        if evidence is None:
            continue
        for revert_pattern in revert_patterns:
            if revert_pattern.search(evidence.comment):
                short_comment = evidence.comment[:80]
                return "revert_or_vandalism", f"{side} comment {short_comment!r}"

    # 3. A disambiguation page.
    for side in ("leader", "follower"):
        title = record[f"{side}_title"]
        for marker in rules.disambiguation_markers:
            if marker in title:
                return "disambiguation", f"{side} title {title!r}"

    # 4. Both edits were tiny.
    sizes = []
    for _, evidence in sides:
        if evidence is not None and evidence.bytes_changed is not None:
            sizes.append(evidence.bytes_changed)

    if len(sizes) == 2:
        both_tiny = True
        for size in sizes:
            if abs(size) > rules.trivial_bytes:
                both_tiny = False
        if both_tiny:
            return (
                "trivial_maintenance",
                f"size changes {sizes[0]:+d} / {sizes[1]:+d} bytes",
            )

    # 5. The stream delivered an edit later than the reorder buffer allows.
    for side, evidence in sides:
        if evidence is None:
            continue
        skew = evidence.stream_skew_seconds
        if skew is not None and abs(skew) > config.join.reorder_seconds:
            return "out_of_order", f"{side} stream skew {skew:.0f}s"

    # 6. The same person made both edits.
    if leader is not None and follower is not None:
        if leader.user and leader.user == follower.user:
            return "same_editor", f"user {leader.user!r} made both edits"

    return "needs_review", ""


def build_sheet(
    sampled: list[dict],
    evidence: dict[tuple[str, str, int], EditEvidence],
    config: Config,
) -> list[dict]:
    rows = []

    for index in range(len(sampled)):
        record = sampled[index]
        leader = evidence.get(edit_key(record, "leader"))
        follower = evidence.get(edit_key(record, "follower"))
        category, why = suggest(record, leader, follower, config)

        # Leave evidence columns empty when the edit wasn't found in the archive.
        leader_user = ""
        leader_comment = ""
        leader_bytes = ""
        if leader is not None:
            leader_user = leader.user
            leader_comment = leader.comment
            leader_bytes = leader.bytes_changed

        follower_user = record["follower_user"]
        follower_comment = ""
        follower_bytes = ""
        if follower is not None:
            follower_user = follower.user
            follower_comment = follower.comment
            follower_bytes = follower.bytes_changed

        rows.append(
            {
                "case_id": f"f{index + 1:03d}",
                "qid": record["qid"],
                "rank": record["rank"],
                "lag_seconds": record["lag_seconds"],
                "leader_wiki": record["leader_wiki"],
                "leader_title": record["leader_title"],
                "leader_user": leader_user,
                "leader_comment": leader_comment,
                "leader_bytes": leader_bytes,
                "follower_wiki": record["follower_wiki"],
                "follower_title": record["follower_title"],
                "follower_user": follower_user,
                "follower_comment": follower_comment,
                "follower_bytes": follower_bytes,
                "suggested_category": category,
                "evidence": why,
                "confirmed_wrong": "",
                "category": "",
                "note": "",
            }
        )

    return rows


def write_sheet(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SHEET_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def summarise_sheet(path: Path) -> dict:
    """Counts of confirmed-wrong cases by category, with their case ids."""
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    reviewed = []
    for row in rows:
        if parse_label(row["confirmed_wrong"]) is not None:
            reviewed.append(row)

    wrong = []
    for row in reviewed:
        if parse_label(row["confirmed_wrong"]):
            wrong.append(row)

    # The reviewer's category wins; otherwise the suggestion stands.
    by_category: dict[str, list[str]] = {}
    agreed = 0
    for row in wrong:
        category = row["category"].strip()
        if category == "":
            category = row["suggested_category"]

        if category not in by_category:
            by_category[category] = []
        by_category[category].append(row["case_id"])

        if category == row["suggested_category"]:
            agreed += 1

    def most_cases_first(item: tuple[str, list[str]]) -> tuple[int, str]:
        name = item[0]
        case_ids = item[1]
        return (-len(case_ids), name)

    categories = []
    for name, case_ids in sorted(by_category.items(), key=most_cases_first):
        categories.append({"category": name, "count": len(case_ids), "cases": case_ids})

    heuristic_agreement = None
    if len(wrong) > 0:
        heuristic_agreement = round(agreed / len(wrong), 4)

    return {
        "candidates": len(rows),
        "reviewed": len(reviewed),
        "confirmed_wrong": len(wrong),
        "categories": categories,
        "heuristic_agreement": heuristic_agreement,
    }
