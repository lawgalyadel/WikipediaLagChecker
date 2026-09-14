# TODO

Results, design notes and the commands to reproduce everything are in the
README. This is what's left.

## 1. Keep the archiver running

```bash
docker compose up -d
```

Or run `python -m WikipediaLagChecker archive` in a terminal that's only used for that.

Propagation numbers need more than 12 hours of continuous archive (6h
watermark + 6h warm-up), and a few days would be better. Don't let the
laptop sleep. Short outages get caught up from the stream when it
reconnects, but long ones leave gaps (`python -m WikipediaLagChecker stats` shows them).

## 2. Label the baseline pairs

Fill in `same_subject` with `y` or `n` for all 200 rows in
`labels/pairs.csv`. Open both links and decide whether the two articles are
about the same thing. Don't go by the language links in the sidebar, since
those come from Wikidata, which is what's being tested. Don't open
`labels/pairs.key.csv` until all rows are done. Then:

```bash
python -m WikipediaLagChecker pairs evaluate
python -m WikipediaLagChecker report
```

To redraw the sample over a longer window (`pairs sample --force`), do it
before labelling, because it replaces the sheet.

## 3. Review failure cases

Once `python -m WikipediaLagChecker join` starts producing records (after the 12h mark):

```bash
python -m WikipediaLagChecker failures sample
```

In `labels/failures.csv`, fill in `confirmed_wrong` for each case and fix
`category` where the suggestion is wrong. Stop after 20 confirmed wrong,
then run `failures summarise` and `report`.

## 4. Final checks

- Re-run everything under "Reproducing the results" in the README over the
  full archive, then `python -m WikipediaLagChecker report`.
- Clone the repo into a new folder and check that `docker compose up` works
  there without changes.
