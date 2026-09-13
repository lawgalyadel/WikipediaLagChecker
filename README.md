# wikilag

Measuring how long it takes an edit about the same subject to propagate from
one Wikipedia language edition to the next, using Wikimedia's live
`recentchange` stream.

**Status:** day-one scaffolding. The archiver runs; the analysis does not
exist yet. Results below are placeholders and will be replaced with measured
numbers — no number appears in this README that did not come from a run.

## Why

The same real-world event is written into one language edition first and the
others follow. Nobody has measured that lag at stream granularity: which
editions lead, which lag, and how often an update never propagates at all.

## Results

_To be filled in from measured runs. Structure fixed now so the experiments
have somewhere to land._

| Metric | Baseline (title match) | Wikidata-linked | Notes |
|---|---|---|---|
| Cross-edition pairs found | — | — | over the same archived window |
| Pair precision / recall | — | — | vs 200 hand-labelled pairs |
| Median propagation lag | — | — | |
| p95 propagation lag | — | — | |

| Pipeline | Value |
|---|---|
| Sustained replay throughput (1 / 2 / 4 workers) | — |
| Peak RSS under replay | — |
| Wikidata cache hit rate | — |
| Events dropped past the 6h watermark | — |
| Undecodable archive lines | — |

## Quickstart

```bash
docker compose up          # archiver runs until stopped, writes to ./data
```

Or without Docker:

```bash
pip install -e ".[dev]"
python -m wikilag archive --max-events 500
python -m wikilag stats
```

## How it works

```
stream.wikimedia.org ──SSE──> archiver ──> data/raw/YYYY-MM-DD-HH.jsonl.gz
                                  │
                                  └──> data/offset.txt   (resume point)

data/raw/*.jsonl.gz ──> replay ──> [entity resolution] ──> [propagation join] ──> results
                        (deterministic)   (day 2-3)            (day 4)
```

The archiver is intentionally dumb: it filters on wiki and namespace, then
writes raw payloads to disk. Nothing is interpreted at capture time, because
an interpretation bug at capture destroys data permanently while the same
bug at analysis time costs one replay.

Everything downstream reads the archive, never the live stream. Partitions
are read in sorted order and lines in file order, so a given archive
produces the same event sequence every run — which is what makes a
before/after measurement mean anything.

## Design notes

**Resume, not restart.** The last event id is persisted after every archived
event, written to a temp file and renamed so a crash cannot leave a
truncated offset. On reconnect it goes back as `Last-Event-ID`.

**Bots are archived, not discarded.** Bots mirror content across editions
mechanically and would swamp the human propagation signal, so they are
excluded at analysis time — but their share of cross-edition edits is a
number worth reporting, and you cannot report what you threw away.

**Malformed data is counted, not dropped silently.** Unparseable timestamps
fall back to wall clock and log a warning; undecodable lines are counted by
`wikilag stats` and feed the drop table above.

## Limitations

- The wiki list in `config/default.toml` is 18 editions, not all 300+.
  Widening it adds volume without adding a finding.
- The 6h watermark means genuinely slow propagation is recorded as
  non-propagation. The rate at which that happens is reported rather than
  hidden.
- Namespace 0 only: talk pages, templates and redirects are out of scope.

## Layout

```
src/wikilag/
  config.py         all tunables, loaded from TOML
  sse.py            SSE parser (pure function over lines — testable offline)
  archiver.py       stream → hourly gzip partitions, with offset resume
  replay.py         deterministic replay over the archive
  logging_setup.py  structlog JSON with a run id on every record
```
