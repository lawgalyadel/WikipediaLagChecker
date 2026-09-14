"""Structured logging.

JSON lines to stdout, with a run id on every record so one run's logs can
be picked out from the rest.

httpx logs through the standard library, so the root handler sends those
records through the same JSON renderer. Otherwise they'd show up as plain
text mixed into the JSON output.
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
    # "INFO" -> logging.INFO (20), etc.
    numeric_level = getattr(logging, level.upper())

    # Records from the standard library (httpx) go through these first.
    stdlib_processors = [structlog.stdlib.add_logger_name]
    stdlib_processors.extend(_SHARED_PROCESSORS)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=stdlib_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(numeric_level)

    # Our own structlog calls go through the shared processors, then get handed
    # to the same handler as above.
    structlog_processors = list(_SHARED_PROCESSORS)
    structlog_processors.append(structlog.stdlib.ProcessorFormatter.wrap_for_formatter)

    structlog.configure(
        processors=structlog_processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        cache_logger_on_first_use=False,
    )

    if run_id:
        resolved = run_id
    else:
        resolved = uuid.uuid4().hex[:12]

    structlog.contextvars.bind_contextvars(run_id=resolved)
    return resolved
