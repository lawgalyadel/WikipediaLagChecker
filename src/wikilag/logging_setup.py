"""Structured logging.

JSON lines to stdout, with a run id bound to every record so a single
archiver run can be isolated from a week of logs. Print statements are not
used anywhere in this package.
"""

from __future__ import annotations

import logging
import uuid

import structlog


def configure(level: str = "INFO", run_id: str | None = None) -> str:
    """Configure structlog and return the run id bound to this process."""
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper()))

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper())
        ),
        cache_logger_on_first_use=True,
    )

    resolved = run_id or uuid.uuid4().hex[:12]
    structlog.contextvars.bind_contextvars(run_id=resolved)
    return resolved
