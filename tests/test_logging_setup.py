import json
import logging

import structlog

from wikilag.logging_setup import configure


def _records(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_package_logs_are_json_with_run_id(capsys):
    run_id = configure("INFO", run_id="abc123")
    structlog.get_logger("wikilag.test").info("thing.happened", count=3)

    (record,) = _records(capsys)
    assert record["event"] == "thing.happened"
    assert record["count"] == 3
    assert record["run_id"] == run_id == "abc123"


def test_third_party_stdlib_logs_are_json_too(capsys):
    # httpx logs "HTTP Request: ..." through the stdlib; it must not break
    # the one-JSON-object-per-line contract.
    configure("INFO", run_id="abc123")
    logging.getLogger("httpx").info("HTTP Request: GET %s", "https://example.org")

    (record,) = _records(capsys)
    assert record["event"] == "HTTP Request: GET https://example.org"
    assert record["logger"] == "httpx"
    assert record["run_id"] == "abc123"


def test_level_filters_both_paths(capsys):
    configure("WARNING", run_id="abc123")
    structlog.get_logger("wikilag.test").info("hidden")
    logging.getLogger("httpx").info("hidden too")
    assert _records(capsys) == []
