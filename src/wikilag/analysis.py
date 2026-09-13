"""Aggregate propagation records and closed windows into reported numbers.

Edition leadership is confounded by volume: English is edited far more
than Hindi, so it is first more often by chance alone. Each edition's lead
rate is therefore reported next to the rate it would have if the leader of
every window it appeared in were drawn uniformly at random (1/n for a
window with n editions). Lift above 1 means it leads more than volume and
co-occurrence explain.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from wikilag.propagation import ItemWindow, PropagationRecord
from wikilag.resolver import percentile


@dataclass
class EditionTally:
    windows: int = 0
    leads: int = 0
    expected_leads: float = 0.0
    position_sum: float = 0.0  # 0 = first, 1 = last, per window
    follower_lags: list[int] = field(default_factory=list)


@dataclass
class LagAggregator:
    lags_by_rank: dict[int, list[int]] = field(default_factory=lambda: defaultdict(list))
    lags_by_pair: dict[tuple[str, str], list[int]] = field(
        default_factory=lambda: defaultdict(list)
    )
    editions: dict[str, EditionTally] = field(
        default_factory=lambda: defaultdict(EditionTally)
    )
    records: int = 0

    def add_record(self, record: PropagationRecord) -> None:
        self.records += 1
        self.lags_by_rank[record.rank].append(record.lag_seconds)
        pair = (record.leader_wiki, record.follower_wiki)
        self.lags_by_pair[pair].append(record.lag_seconds)

    def add_window(self, window: ItemWindow) -> None:
        """Only windows reaching a second edition say anything about order."""
        order = list(window.human_editions.items())
        if len(order) < 2:
            return
        leader_time = order[0][1][0]
        last = len(order) - 1
        for position, (wiki, (edit_time, _)) in enumerate(order):
            tally = self.editions[wiki]
            tally.windows += 1
            tally.expected_leads += 1 / len(order)
            tally.position_sum += position / last
            if position == 0:
                tally.leads += 1
            else:
                tally.follower_lags.append(edit_time - leader_time)

    def summary(self, min_samples: int) -> dict:
        return {
            "records": self.records,
            "lag_by_rank": {
                str(rank): _lag_summary(lags)
                for rank, lags in sorted(self.lags_by_rank.items())
            },
            "lag_by_pair": [
                {"leader": leader, "follower": follower, **_lag_summary(lags)}
                for (leader, follower), lags in sorted(
                    self.lags_by_pair.items(), key=lambda item: (-len(item[1]), item[0])
                )
                if len(lags) >= min_samples
            ],
            "pairs_below_min_samples": sum(
                1 for lags in self.lags_by_pair.values() if len(lags) < min_samples
            ),
            "editions": sorted(
                (
                    _edition_summary(wiki, tally)
                    for wiki, tally in self.editions.items()
                    if tally.windows >= min_samples
                ),
                key=lambda row: (-row["lead_lift"], row["wiki"]),
            ),
        }


def _lag_summary(lags: list[int]) -> dict:
    return {
        "n": len(lags),
        "median_seconds": percentile(lags, 0.5),
        "p95_seconds": percentile(lags, 0.95),
    }


def _edition_summary(wiki: str, tally: EditionTally) -> dict:
    return {
        "wiki": wiki,
        "windows": tally.windows,
        "leads": tally.leads,
        "lead_rate": round(tally.leads / tally.windows, 4),
        "expected_lead_rate": round(tally.expected_leads / tally.windows, 4),
        "lead_lift": round(tally.leads / tally.expected_leads, 3),
        "mean_position": round(tally.position_sum / tally.windows, 4),
        "median_follow_lag_seconds": percentile(tally.follower_lags, 0.5),
    }
