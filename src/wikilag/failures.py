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

from wikilag.config import Config
from wikilag.pairs import parse_label

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
    return (
        record[f"{side}_wiki"],
        record[f"{side}_title"],
        int(record[f"{side}_timestamp"]),
    )


def read_records(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def sample_records(records: list[dict], size: int, seed: int) -> list[dict]:
    ordered = sorted(records, key=lambda r: (r["qid"], int(r["rank"])))
    return random.Random(seed).sample(ordered, min(size, len(ordered)))


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
        if key not in wanted or key in found:
            continue
        length = event.get("length") or {}
        old, new = length.get("old"), length.get("new")
        skew = None
        dt = (event.get("meta") or {}).get("dt")
        if isinstance(dt, str):
            try:
                skew = (
                    datetime.fromisoformat(dt.replace("Z", "+00:00")).timestamp() - key[2]
                )
            except ValueError:
                skew = None
        found[key] = EditEvidence(
            user=event.get("user", ""),
            comment=event.get("comment", ""),
            bytes_changed=None if new is None else new - (old or 0),
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

    bot_pattern = re.compile(rules.bot_user_pattern)
    for side, evidence in sides:
        if evidence and not evidence.bot and bot_pattern.search(evidence.user):
            return "bot_slipped_filter", f"{side} user {evidence.user!r}"

    reverts = [re.compile(pattern) for pattern in rules.revert_comment_patterns]
    for side, evidence in sides:
        if evidence and any(p.search(evidence.comment) for p in reverts):
            return "revert_or_vandalism", f"{side} comment {evidence.comment[:80]!r}"

    for side in ("leader", "follower"):
        title = record[f"{side}_title"]
        if any(marker in title for marker in rules.disambiguation_markers):
            return "disambiguation", f"{side} title {title!r}"

    sizes = [e.bytes_changed for _, e in sides if e and e.bytes_changed is not None]
    if len(sizes) == 2 and all(abs(size) <= rules.trivial_bytes for size in sizes):
        return "trivial_maintenance", f"size changes {sizes[0]:+d} / {sizes[1]:+d} bytes"

    for side, evidence in sides:
        skew = evidence.stream_skew_seconds if evidence else None
        if skew is not None and abs(skew) > config.join.reorder_seconds:
            return "out_of_order", f"{side} stream skew {skew:.0f}s"

    if leader and follower and leader.user and leader.user == follower.user:
        return "same_editor", f"user {leader.user!r} made both edits"

    return "needs_review", ""


def build_sheet(
    sampled: list[dict],
    evidence: dict[tuple[str, str, int], EditEvidence],
    config: Config,
) -> list[dict]:
    rows = []
    for index, record in enumerate(sampled, 1):
        leader = evidence.get(edit_key(record, "leader"))
        follower = evidence.get(edit_key(record, "follower"))
        category, why = suggest(record, leader, follower, config)
        rows.append(
            {
                "case_id": f"f{index:03d}",
                "qid": record["qid"],
                "rank": record["rank"],
                "lag_seconds": record["lag_seconds"],
                "leader_wiki": record["leader_wiki"],
                "leader_title": record["leader_title"],
                "leader_user": leader.user if leader else "",
                "leader_comment": leader.comment if leader else "",
                "leader_bytes": "" if not leader else leader.bytes_changed,
                "follower_wiki": record["follower_wiki"],
                "follower_title": record["follower_title"],
                "follower_user": follower.user if follower else record["follower_user"],
                "follower_comment": follower.comment if follower else "",
                "follower_bytes": "" if not follower else follower.bytes_changed,
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

    reviewed = [row for row in rows if parse_label(row["confirmed_wrong"]) is not None]
    wrong = [row for row in reviewed if parse_label(row["confirmed_wrong"])]
    by_category: dict[str, list[str]] = {}
    for row in wrong:
        category = row["category"].strip() or row["suggested_category"]
        by_category.setdefault(category, []).append(row["case_id"])

    agreed = sum(
        1
        for row in wrong
        if (row["category"].strip() or row["suggested_category"])
        == row["suggested_category"]
    )
    return {
        "candidates": len(rows),
        "reviewed": len(reviewed),
        "confirmed_wrong": len(wrong),
        "categories": [
            {"category": name, "count": len(ids), "cases": ids}
            for name, ids in sorted(
                by_category.items(), key=lambda kv: (-len(kv[1]), kv[0])
            )
        ],
        "heuristic_agreement": round(agreed / len(wrong), 4) if wrong else None,
    }
