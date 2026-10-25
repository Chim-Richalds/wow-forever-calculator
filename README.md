# WoW Forever Talent Value Calculator

A local tool that works out what each talent is worth: the stat with the talent minus the stat without it. It reads the game's own data tables (from Wago), keeps every build versioned, and says plainly what it cannot model.

## Run it

```
cd wow-forever-calculator
PYTHONPATH=src python3 -m wowcalc.ui          # opens http://127.0.0.1:8765
```

Plain Python 3 with no packages (developed and tested on 3.13). Your own files go in `data/external/` (`base_stats.tsv`, `sheet_base.tsv`, `stat_rates.json` and the rest; see `BACKLOG.md` for what each is for).

## Get or update the game data

```
PYTHONPATH=src python3 -m wowcalc.update status      # what is installed and what is available
PYTHONPATH=src python3 -m wowcalc.update prepare     # fetch and check the latest build (waits in staging)
PYTHONPATH=src python3 -m wowcalc.update apply       # install it and make it active
PYTHONPATH=src python3 -m wowcalc.update complete    # add tables an installed build lacks
PYTHONPATH=src python3 -m wowcalc.diff               # what changed between builds
PYTHONPATH=src python3 -m wowcalc.rates fit          # refit the stat rates
```

Raw tables go in `data/raw/<build>/` with hashes in `data/manifests/`. They are not kept in git.

## Icons

```
PYTHONPATH=src python3 -m wowcalc.icons tables
PYTHONPATH=src python3 -m wowcalc.icons import FOLDER --listfile community-listfile.csv --only-needed
PYTHONPATH=src python3 -m wowcalc.icons status
```

## Tests

```
python3 -X dev -W error::ResourceWarning -m unittest discover -s tests
```

Tests that need the real downloaded data are skipped unless `WOWCALC_DATA` points at it.

## Layout

- `src/wowcalc/`: the code (`effects.py` decodes talents, `stats.py` builds the sheet, `ui.py` and `ui_page.html` are the page).
- `data/external/`: hand-collected inputs (panel readings, fitted rates, spec names).
- `tests/`: unit tests.
- `BACKLOG.md`: what is pending, and what needs the game servers.
