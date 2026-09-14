"""Minimal server-sent-events parser.

Written as a function over decoded lines instead of something that owns a
socket, so it can be tested without a network connection.

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

    A block ends at a blank line. An incomplete block at the end is dropped,
    since the archiver resumes from the last complete id anyway.
    """
    event_type = "message"
    data_parts: list[str] = []
    event_id: str | None = None

    for raw_line in lines:
        line = raw_line.rstrip("\n")
        line = line.rstrip("\r")

        # Lines starting with a colon are comments (keep-alives).
        if line.startswith(":"):
            continue

        # A blank line means the current block is finished.
        if line == "":
            if len(data_parts) > 0:
                message = SSEMessage(
                    event=event_type,
                    data="\n".join(data_parts),
                    id=event_id,
                )
                yield message
            event_type = "message"
            data_parts = []
            continue

        # Every other line looks like "field: value".
        colon_position = line.find(":")
        if colon_position == -1:
            field = line
            value = ""
        else:
            field = line[:colon_position]
            value = line[colon_position + 1 :]

        # The spec allows one optional space after the colon.
        if value.startswith(" "):
            value = value[1:]

        if field == "event":
            event_type = value
        elif field == "data":
            data_parts.append(value)
        elif field == "id":
            event_id = value
