# Start here

Read in this order. Everything else is scaffolding you can ignore until it
breaks.

## 1. Do this first (today, before reading any code)

```bash
docker compose up
```

Leave it running. Every hour it runs is an hour of data you have on day
five. If Docker is a hassle:

```bash
pip install -e ".[dev]"
python -m wikilag archive --max-events 20   # smoke test, then Ctrl-C
python -m wikilag stats
```

Two things to check on that first real run, because I couldn't reach the
stream to verify them:

- Is the wiki field in the payload `wiki` (`enwiki`) or `server_name`
  (`en.wikipedia.org`)? Fix `should_keep` / the config list if it's the
  latter, otherwise you'll archive nothing.
- Does the SSE `id` come back as a JSON offset array? That's what makes
  resume work after a disconnect.

## 2. The files that matter

| File | Why you care |
|---|---|
| `src/wikilag/archiver.py` | The thing that runs today. Hourly gzip partitions + offset resume. |
| `src/wikilag/replay.py` | Deterministic replay. Every experiment goes through here, never the live stream. |
| `config/default.toml` | Wiki list and watermark. The two decisions you should make deliberately. |
| `README.md` | Empty results tables, waiting for measured numbers. |

## 3. The files that are just plumbing

`sse.py` (parser, done and tested), `config.py`, `logging_setup.py`,
`__main__.py`, `Dockerfile`, `docker-compose.yml`, `.github/workflows/ci.yml`.
Written once, unlikely to change.

## 4. What doesn't exist yet

- **Days 2–3** — Wikidata sitelink resolution with a bounded LRU cache.
  The distinctive part. Report hit rate from the start.
- **Day 4** — the propagation join, bounded state at the 6h watermark.
- **Day 5** — naive title-match baseline, 200 hand-labelled pairs,
  scaling numbers at 1/2/4 workers.
- **Day 6** — 20 wrong pairs categorised, README filled in.

## 5. Two decisions to make before day 2

They live in `config/default.toml` and are annoying to change later:

- **Which editions.** Currently 18, deliberately not all-European. Widening
  adds volume without adding a finding.
- **The watermark.** Currently 6h. Slower propagation than that gets
  recorded as non-propagation, so the number you pick shapes the result.
