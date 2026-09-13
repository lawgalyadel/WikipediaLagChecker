"""Minimal server-sent-events parser.

Deliberately written as a pure function over an iterable of decoded lines
rather than something that owns a socket. That means the parser is fully
testable without a network, which is the only reason it has tests on day
one instead of day never.

Only the fields this project needs are handled: `id`, `event`, `data`.
Comment lines (starting ':') are ignored, which is how the Wikimedia
endpoint sends its keep-alives.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class SSEMessage:
    event: str
    data: str
    id: str | None


def parse_sse(lines: Iterable[str]) -> Iterator[SSEMessage]:
    """Yield one SSEMessage per complete event block.

    A block ends at a blank line. Incomplete trailing blocks are dropped,
    which is correct: a half-received event on a dropped connection is not
    an event, and we will resume from the last complete id.
    """
    event_type = "message"
    data_parts: list[str] = []
    event_id: str | None = None

    for raw_line in lines:
        line = raw_line.rstrip("\n").rstrip("\r")

        if line.startswith(":"):
            continue

        if line == "":
            if data_parts:
                yield SSEMessage(
                    event=event_type,
                    data="\n".join(data_parts),
                    id=event_id,
                )
            event_type = "message"
            data_parts = []
            continue

        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value

        if field == "event":
            event_type = value
        elif field == "data":
            data_parts.append(value)
        elif field == "id":
            event_id = value
