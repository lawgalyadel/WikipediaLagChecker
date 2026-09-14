"""The propagation join: edits grouped by Wikidata item across editions.

A window opens on an item's first edit and stays open for the watermark.
Within it, each human edition's first edit is recorded in edit-time order;
when the 2nd, 3rd and 5th edition arrive a `PropagationRecord` is emitted
with the lag from the leading edition.

State is bounded three ways: open windows expire at the watermark, closed
windows are remembered only for the late horizon (long enough to count
follow-ups that missed the window), and the reorder buffer holds at most
`reorder_seconds` of edits.

Definitions that affect the numbers:

- Time is edit time (`timestamp`), not arrival time. The clock is the
  largest edit time seen, so replay speed has no effect on results.
- Bot edits keep a window open and count toward the bot share, but never
  lead or follow: propagation is measured over human edits only.
- A new human edition touching an item after its window closed is a late
  follow-up. It doesn't reopen the window, or it would become the leader.
- Windows opening within `warmup_seconds` of the first archived edit may
  have had edits before capture began. They emit nothing and are excluded
  from rates, but are counted.
"""

from __future__ import annotations

import heapq
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from WikipediaLagChecker.config import JoinConfig
from WikipediaLagChecker.events import Edit


@dataclass(frozen=True, slots=True)
class PropagationRecord:
    qid: str
    rank: int
    leader_wiki: str
    leader_title: str
    leader_timestamp: int
    follower_wiki: str
    follower_title: str
    follower_timestamp: int
    follower_user: str

    @property
    def lag_seconds(self) -> int:
        return self.follower_timestamp - self.leader_timestamp


@dataclass(slots=True)
class ItemWindow:
    qid: str
    opened_at: int
    in_warmup: bool
    # wiki -> (edit time, title) of that edition's first human edit, in order
    human_editions: dict[str, tuple[int, str]] = field(default_factory=dict)
    all_editions: set[str] = field(default_factory=set)
    human_edits: int = 0
    bot_edits: int = 0


@dataclass(slots=True)
class ClosedWindow:
    closed_at: int
    editions: frozenset[str]
    late: bool = False


@dataclass
class JoinStats:
    edits_in: int = 0
    without_item: int = 0
    human_edits: int = 0
    bot_edits: int = 0
    out_of_order: int = 0  # arrived after edits later than it were processed
    windows_opened: int = 0
    windows_closed: int = 0
    windows_open_at_end: int = 0
    warmup_windows: int = 0
    closed_with_human_edit: int = 0  # the denominator for every rate below
    reached_rank: dict[int, int] = field(default_factory=dict)
    late_followup_items: int = 0
    late_followup_edits: int = 0
    cross_edition_items: int = 0
    cross_edition_human_edits: int = 0
    cross_edition_bot_edits: int = 0
    peak_open_windows: int = 0
    peak_closed_remembered: int = 0
    peak_reorder_buffer: int = 0

    @property
    def bot_share_of_cross_edition_edits(self) -> float | None:
        total = self.cross_edition_human_edits + self.cross_edition_bot_edits
        if total == 0:
            return None
        return self.cross_edition_bot_edits / total

    @property
    def late_followup_rate(self) -> float | None:
        if self.closed_with_human_edit == 0:
            return None
        return self.late_followup_items / self.closed_with_human_edit


class PropagationJoin:
    def __init__(
        self,
        config: JoinConfig,
        on_record: Callable[[PropagationRecord], None],
        on_close: Callable[[ItemWindow], None] | None = None,
    ) -> None:
        self._config = config
        self._on_record = on_record
        self._on_close = on_close
        self._emit_ranks = set(config.emit_ranks)

        # Reorder buffer: a heap of (edit time, sequence number, edit, qid).
        self._buffer: list[tuple[int, int, Edit, str]] = []
        self._sequence = 0

        self._clock: int | None = None  # largest edit time pushed
        self._processed_upto: int | None = None  # largest edit time processed
        self._first_timestamp: int | None = None

        # Both dicts keep insertion order, so the first entry is the oldest.
        self._open: OrderedDict[str, ItemWindow] = OrderedDict()
        self._closed: OrderedDict[str, ClosedWindow] = OrderedDict()

        reached_rank = {}
        for rank in config.emit_ranks:
            reached_rank[rank] = 0
        self.stats = JoinStats(reached_rank=reached_rank)

    def push(self, edit: Edit, qid: str | None) -> None:
        self.stats.edits_in += 1

        if qid is None:
            self.stats.without_item += 1
            return

        # The earliest edit time, not the first to arrive: arrival order is
        # exactly what the reorder buffer does not trust.
        if self._first_timestamp is None or edit.timestamp < self._first_timestamp:
            self._first_timestamp = edit.timestamp

        if self._clock is None or edit.timestamp > self._clock:
            self._clock = edit.timestamp

        # The sequence number breaks ties between edits with the same
        # timestamp, so they come out in the order they arrived.
        entry = (edit.timestamp, self._sequence, edit, qid)
        heapq.heappush(self._buffer, entry)
        self._sequence += 1

        if len(self._buffer) > self.stats.peak_reorder_buffer:
            self.stats.peak_reorder_buffer = len(self._buffer)

        # Anything at least `reorder_seconds` older than the clock is safe to
        # process now: nothing earlier should still be on its way.
        release_before = self._clock - self._config.reorder_seconds
        while len(self._buffer) > 0 and self._buffer[0][0] <= release_before:
            oldest = heapq.heappop(self._buffer)
            ready_edit = oldest[2]
            ready_qid = oldest[3]
            self._process(ready_edit, ready_qid)

    def finish(self) -> None:
        """Drain the reorder buffer. Windows still open are counted, not closed:
        their follow-ups may simply not have happened yet."""
        while len(self._buffer) > 0:
            oldest = heapq.heappop(self._buffer)
            ready_edit = oldest[2]
            ready_qid = oldest[3]
            self._process(ready_edit, ready_qid)

        self.stats.windows_open_at_end = len(self._open)

    def _process(self, edit: Edit, qid: str) -> None:
        now = edit.timestamp

        if self._processed_upto is not None and now < self._processed_upto:
            self.stats.out_of_order += 1
        else:
            self._processed_upto = now
        self._expire(self._processed_upto)

        if edit.bot:
            self.stats.bot_edits += 1
        else:
            self.stats.human_edits += 1

        # If this item's window already closed, the edit can only be a late
        # follow-up. It never opens a new window.
        closed = self._closed.get(qid)
        if closed is not None:
            is_new_human_edition = not edit.bot and edit.wiki not in closed.editions
            if is_new_human_edition:
                self.stats.late_followup_edits += 1
                if not closed.late:
                    closed.late = True
                    self.stats.late_followup_items += 1
            return

        window = self._open.get(qid)
        if window is None:
            window = self._open_window(qid, now)

        window.all_editions.add(edit.wiki)

        if edit.bot:
            window.bot_edits += 1
            return

        window.human_edits += 1

        # Only the first human edit from each edition counts.
        if edit.wiki in window.human_editions:
            return

        window.human_editions[edit.wiki] = (now, edit.title)
        rank = len(window.human_editions)

        if rank not in self._emit_ranks:
            return
        if window.in_warmup:
            return

        # The leader is the first edition that was added to the window.
        leader_wiki = None
        for wiki in window.human_editions:
            leader_wiki = wiki
            break
        leader_time, leader_title = window.human_editions[leader_wiki]

        record = PropagationRecord(
            qid=qid,
            rank=rank,
            leader_wiki=leader_wiki,
            leader_title=leader_title,
            leader_timestamp=leader_time,
            follower_wiki=edit.wiki,
            follower_title=edit.title,
            follower_timestamp=now,
            follower_user=edit.user,
        )
        self._on_record(record)

    def _open_window(self, qid: str, now: int) -> ItemWindow:
        assert self._first_timestamp is not None

        warmup_ends_at = self._first_timestamp + self._config.warmup_seconds
        in_warmup = now < warmup_ends_at

        window = ItemWindow(qid=qid, opened_at=now, in_warmup=in_warmup)
        self._open[qid] = window

        self.stats.windows_opened += 1
        if in_warmup:
            self.stats.warmup_windows += 1
        if len(self._open) > self.stats.peak_open_windows:
            self.stats.peak_open_windows = len(self._open)

        return window

    def _expire(self, now: int) -> None:
        # Close open windows that have been open longer than the watermark.
        # The oldest window is always first, so stop at the first one that
        # is still inside the watermark.
        closes_before = now - self._config.watermark_seconds
        while len(self._open) > 0:
            oldest_qid = None
            for qid in self._open:
                oldest_qid = qid
                break
            oldest_window = self._open[oldest_qid]

            if oldest_window.opened_at > closes_before:
                break

            del self._open[oldest_qid]
            self._close(oldest_window, now)

        # Forget closed windows once they're past the late horizon.
        forget_before = now - self._config.late_horizon_seconds
        while len(self._closed) > 0:
            oldest_qid = None
            for qid in self._closed:
                oldest_qid = qid
                break
            oldest_closed = self._closed[oldest_qid]

            if oldest_closed.closed_at > forget_before:
                break

            del self._closed[oldest_qid]

    def _close(self, window: ItemWindow, now: int) -> None:
        stats = self.stats
        stats.windows_closed += 1

        # Late follow-ups are new *human* editions, matching what leads.
        human_wikis = frozenset(window.human_editions)
        self._closed[window.qid] = ClosedWindow(closed_at=now, editions=human_wikis)
        if len(self._closed) > stats.peak_closed_remembered:
            stats.peak_closed_remembered = len(self._closed)

        if len(window.all_editions) >= 2:
            stats.cross_edition_items += 1
            stats.cross_edition_human_edits += window.human_edits
            stats.cross_edition_bot_edits += window.bot_edits

        if window.in_warmup:
            return
        if len(window.human_editions) == 0:
            return

        stats.closed_with_human_edit += 1
        for rank in stats.reached_rank:
            if len(window.human_editions) >= rank:
                stats.reached_rank[rank] += 1

        if self._on_close is not None:
            self._on_close(window)
