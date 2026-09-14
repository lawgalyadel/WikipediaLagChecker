"""Aggregate propagation records and closed windows into reported numbers.

Edition leadership is confounded by volume: English is edited far more
than Hindi, so it is first more often by chance alone. Each edition's lead
rate is therefore reported next to the rate it would have if the leader of
every window it appeared in were drawn uniformly at random (1/n for a
window with n editions). Lift above 1 means it leads more than volume and
co-occurrence explain.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from WikipediaLagChecker.propagation import ItemWindow, PropagationRecord
from WikipediaLagChecker.resolver import percentile


@dataclass
class EditionTally:
    windows: int = 0
    leads: int = 0
    expected_leads: float = 0.0
    position_sum: float = 0.0  # 0 = first, 1 = last, per window
    follower_lags: list[int] = field(default_factory=list)


@dataclass
class LagAggregator:
    # rank -> list of lags in seconds
    lags_by_rank: dict[int, list[int]] = field(default_factory=dict)
    # (leader wiki, follower wiki) -> list of lags in seconds
    lags_by_pair: dict[tuple[str, str], list[int]] = field(default_factory=dict)
    # wiki -> running totals for that edition
    editions: dict[str, EditionTally] = field(default_factory=dict)
    records: int = 0

    def add_record(self, record: PropagationRecord) -> None:
        self.records += 1

        if record.rank not in self.lags_by_rank:
            self.lags_by_rank[record.rank] = []
        self.lags_by_rank[record.rank].append(record.lag_seconds)

        pair = (record.leader_wiki, record.follower_wiki)
        if pair not in self.lags_by_pair:
            self.lags_by_pair[pair] = []
        self.lags_by_pair[pair].append(record.lag_seconds)

    def add_window(self, window: ItemWindow) -> None:
        """Only windows reaching a second edition say anything about order."""
        # human_editions keeps the order editions arrived in.
        wikis_in_order = list(window.human_editions.keys())
        edition_count = len(wikis_in_order)
        if edition_count < 2:
            return

        leader_wiki = wikis_in_order[0]
        leader_time = window.human_editions[leader_wiki][0]
        last_position = edition_count - 1

        for position in range(edition_count):
            wiki = wikis_in_order[position]
            edit_time = window.human_editions[wiki][0]

            if wiki not in self.editions:
                self.editions[wiki] = EditionTally()
            tally = self.editions[wiki]

            tally.windows += 1
            tally.expected_leads += 1 / edition_count
            tally.position_sum += position / last_position

            if position == 0:
                tally.leads += 1
            else:
                lag = edit_time - leader_time
                tally.follower_lags.append(lag)

    def summary(self, min_samples: int) -> dict:
        # Lag by rank, in rank order.
        lag_by_rank = {}
        for rank in sorted(self.lags_by_rank.keys()):
            lags = self.lags_by_rank[rank]
            lag_by_rank[str(rank)] = _lag_summary(lags)

        # Lag by language pair: most records first, then alphabetical.
        sorted_pairs = sorted(self.lags_by_pair.items(), key=_pair_sort_key)
        lag_by_pair = []
        pairs_below_min_samples = 0
        for pair, lags in sorted_pairs:
            if len(lags) >= min_samples:
                leader, follower = pair
                row = {"leader": leader, "follower": follower}
                row.update(_lag_summary(lags))
                lag_by_pair.append(row)
        for lags in self.lags_by_pair.values():
            if len(lags) < min_samples:
                pairs_below_min_samples += 1

        # Editions with enough windows: highest lift first, then alphabetical.
        edition_rows = []
        for wiki, tally in self.editions.items():
            if tally.windows >= min_samples:
                edition_rows.append(_edition_summary(wiki, tally))
        edition_rows.sort(key=_edition_sort_key)

        return {
            "records": self.records,
            "lag_by_rank": lag_by_rank,
            "lag_by_pair": lag_by_pair,
            "pairs_below_min_samples": pairs_below_min_samples,
            "editions": edition_rows,
        }


def _pair_sort_key(item: tuple[tuple[str, str], list[int]]) -> tuple:
    pair = item[0]
    lags = item[1]
    return (-len(lags), pair)


def _edition_sort_key(row: dict) -> tuple:
    return (-row["lead_lift"], row["wiki"])


def _lag_summary(lags: list[int]) -> dict:
    return {
        "n": len(lags),
        "median_seconds": percentile(lags, 0.5),
        "p95_seconds": percentile(lags, 0.95),
    }


def _edition_summary(wiki: str, tally: EditionTally) -> dict:
    lead_rate = tally.leads / tally.windows
    expected_lead_rate = tally.expected_leads / tally.windows
    lead_lift = tally.leads / tally.expected_leads
    mean_position = tally.position_sum / tally.windows

    return {
        "wiki": wiki,
        "windows": tally.windows,
        "leads": tally.leads,
        "lead_rate": round(lead_rate, 4),
        "expected_lead_rate": round(expected_lead_rate, 4),
        "lead_lift": round(lead_lift, 3),
        "mean_position": round(mean_position, 4),
        "median_follow_lag_seconds": percentile(tally.follower_lags, 0.5),
    }
