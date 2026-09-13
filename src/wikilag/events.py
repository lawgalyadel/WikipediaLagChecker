"""Raw stream payloads to typed edits.

The archive holds raw payloads; this is the single place they are
interpreted. Everything downstream works on `Edit`, never on dicts, so a
payload quirk is fixed here once.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from wikilag.config import Config


@dataclass(frozen=True, slots=True)
class Edit:
    event_id: str
    wiki: str
    title: str
    timestamp: int  # when the edit was made (rc_timestamp), unix seconds
    bot: bool
    change_type: str
    user: str
    revision: int | None

    @property
    def key(self) -> tuple[str, str]:
        return (self.wiki, self.title)


@dataclass
class EventCounts:
    """What happened to every raw event, so drops are reported, not hidden."""

    seen: int = 0
    kept: int = 0
    duplicate: int = 0
    malformed: int = 0
    filtered: dict[str, int] = field(default_factory=dict)

    def drop(self, reason: str) -> None:
        self.filtered[reason] = self.filtered.get(reason, 0) + 1


class RecentIds:
    """Bounded memory of recently seen event ids.

    Duplicates come from restarts, which re-archive at most one flush
    interval plus whatever the stream replays on resume, so they sit close
    together. A window avoids an id set that grows with the archive.
    """

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._ids: OrderedDict[str, None] = OrderedDict()

    def seen_before(self, event_id: str) -> bool:
        if event_id in self._ids:
            return True
        self._ids[event_id] = None
        if len(self._ids) > self._capacity:
            self._ids.popitem(last=False)
        return False


def to_edit(event: dict) -> Edit | None:
    """Typed edit from a raw payload, or None if required fields are missing."""
    try:
        revision = event.get("revision") or {}
        return Edit(
            event_id=event["meta"]["id"],
            wiki=event["wiki"],
            title=event["title"],
            timestamp=int(event["timestamp"]),
            bot=bool(event.get("bot", False)),
            change_type=event["type"],
            user=event.get("user", ""),
            revision=revision.get("new"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def edits(
    events: Iterable[dict], config: Config, counts: EventCounts | None = None
) -> Iterator[Edit]:
    """Filter and deduplicate raw events into article edits, in archive order.

    Bots are kept: they are excluded at analysis time so their share can be
    reported. `counts` is filled in as a side effect when given.
    """
    counts = counts if counts is not None else EventCounts()
    recent = RecentIds(config.events.dedupe_window_events)
    wikis = set(config.filters.wikis)
    namespaces = set(config.filters.namespaces)
    change_types = set(config.events.change_types)

    for event in events:
        counts.seen += 1
        edit = to_edit(event)
        if edit is None:
            counts.malformed += 1
            continue
        if edit.wiki not in wikis:
            counts.drop("wiki")
            continue
        if event.get("namespace") not in namespaces:
            counts.drop("namespace")
            continue
        if edit.change_type not in change_types:
            counts.drop(f"type:{edit.change_type}")
            continue
        if recent.seen_before(edit.event_id):
            counts.duplicate += 1
            continue
        counts.kept += 1
        yield edit
