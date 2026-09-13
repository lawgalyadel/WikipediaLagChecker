# Start here

The pipeline is complete. What remains is data, and two jobs that need a
person. `README.md` holds the results, the design notes and the
reproduction commands.

## 1. Keep the archiver running

```bash
docker compose up -d
```

Or, without Docker, leave this running in a terminal that nothing else uses:

```bash
python -m wikilag archive
```

Propagation numbers need more than 12 hours of continuous archive (6h
watermark plus 6h warm-up), and they get better with days. Stop the machine
sleeping. Short outages are caught up from the stream on resume, but long
ones leave gaps, and `wikilag stats` reports them.

## 2. Label the baseline pairs

Open `labels/pairs.csv` and fill `same_subject` with `y` or `n` for all 200
rows. Open both URLs and judge whether the two articles are about the same
subject. Don't rely on the interlanguage links in the sidebar: those come
from Wikidata, the method being evaluated. The sheet is blind on purpose, so
don't open `labels/pairs.key.csv` until you're done. Then run:

```bash
python -m wikilag pairs evaluate
python -m wikilag report
```

If you redraw the sample over a longer window (`pairs sample --force`), do
it before labelling. Redrawing replaces the sheet.

## 3. Review failure cases

Once `wikilag join` emits records (after the 12h mark), run:

```bash
python -m wikilag failures sample
```

In `labels/failures.csv`, mark `confirmed_wrong` for each case and set
`category` where the suggestion is wrong. Stop once 20 cases are confirmed
wrong. Then run `failures summarise` and `report`.

## 4. Before calling it done

Re-run the full sequence under "Reproducing the results" in the README over
the whole archive, then run `wikilag report`. Clone the repo into a fresh
directory and check that `docker compose up` works there without edits.
