"""Structured logging.

JSON lines to stdout, with a run id bound to every record so a single
archiver run can be isolated from a week of logs. Print statements are not
used anywhere in this package.

Third-party libraries (httpx) log through the standard library, so the
root handler renders their records through the same JSON pipeline rather
than letting them interleave plain text with the structured stream.
"""

from __future__ import annotations

import logging
import sys
import uuid

import structlog

_SHARED_PROCESSORS: list = [
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
]


def configure(level: str = "INFO", run_id: str | None = None) -> str:
    """Configure structlog and return the run id bound to this process."""
    numeric_level = getattr(logging, level.upper())

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=[structlog.stdlib.add_logger_name, *_SHARED_PROCESSORS],
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(numeric_level)

    structlog.configure(
        processors=[
            *_SHARED_PROCESSORS,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        cache_logger_on_first_use=False,
    )

    resolved = run_id or uuid.uuid4().hex[:12]
    structlog.contextvars.bind_contextvars(run_id=resolved)
    return resolved
