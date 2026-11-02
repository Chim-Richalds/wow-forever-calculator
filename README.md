# WoW Forever Talent Value Calculator

A local tool that works out what each talent is worth: the stat with the talent minus the stat without it. It reads the game's own data tables (from Wago), keeps every build versioned, and says plainly what it cannot model.

## Run it

```
cd wow-forever-calculator
PYTHONPATH=src python3 -m wowcalc.ui          # opens http://127.0.0.1:8765
```

Plain Python 3 with no packages (developed and tested on 3.13). Your own files go in `data/external/` (`base_stats.tsv`, `sheet_base.tsv`, `stat_rates.json` and the rest; see `BACKLOG.md` for what each is for).

## Get or update the game data

In the page: **Data and confidence → Update to the newest build** checks, downloads, validates and installs the newest build, then reloads. A build that fails validation is never installed; new warnings ask for confirmation first. The same steps from the command line:

```
PYTHONPATH=src python3 -m wowcalc.update status      # what is installed and what is available
PYTHONPATH=src python3 -m wowcalc.update prepare     # fetch and check the latest build (waits in staging)
PYTHONPATH=src python3 -m wowcalc.update apply       # install it and make it active
PYTHONPATH=src python3 -m wowcalc.update complete    # add tables an installed build lacks
PYTHONPATH=src python3 -m wowcalc.diff               # what changed between builds
PYTHONPATH=src python3 -m wowcalc.rates fit          # refit the stat rates
PYTHONPATH=src python3 -m wowcalc.talentinfo unmodelled --out docs/unmodelled_effects.tsv   # what is not valued yet
PYTHONPATH=src python3 -m wowcalc.spells audit --out docs/spell-modifier-audit.tsv          # talents that change spells, checked against their text
```

Raw tables go in `data/raw/<build>/` with hashes in `data/manifests/`. They are not kept in git.

Blizzard changes talents with **server hotfixes** between builds. Tables are downloaded with those hotfixes applied (Wago's `useHotfixes=1`), and rows a hotfix deletes are removed by the calculator (Wago's export keeps them). A dataset with hotfixes is named after the build and the newest hotfix it includes, e.g. `1.60.1.70707-hf202610311059`; a newer hotfix set is an update like a newer build. Reading the hotfix list takes a few minutes (Wago serves it 25 rows a page).

## Icons

```
PYTHONPATH=src python3 -m wowcalc.icons import FOLDER --listfile community-listfile.csv --only-needed
PYTHONPATH=src python3 -m wowcalc.icons import FOLDER --talent-names   # images saved as 'turn evil.png'
PYTHONPATH=src python3 -m wowcalc.icons status
```

## Class spells

Each class's spells are its spellbook (`data/external/spellbooks.tsv`, from wowforevertalents.com/abilities, which follows the current Forever build and its hotfixes) plus its talents. Rows flagged `removed` are ignored. The client also carries spells WoW Forever does not use (Season of Discovery runes, old poison ranks); those are left out. Edit the file if a class's spellbook changes.

## Tree backgrounds

One image per specialization behind its tree, from Wowpedia's original-style talent backgrounds (game art, kept in `data/backgrounds/`, not in git). The wiki's image host refuses automated downloads, so save them in a browser and import the folder:

```
PYTHONPATH=src python3 -m wowcalc.backgrounds urls              # where each of the 27 images is
PYTHONPATH=src python3 -m wowcalc.backgrounds import FOLDER     # "Talents background - mage Frost.png" or "mage frost.png"
PYTHONPATH=src python3 -m wowcalc.backgrounds status
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
