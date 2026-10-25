"""Probe which additional DB2 tables Wago.Tools exports for a build. Read-only.

Round 1 (done) found class/tree mapping, unlock rules and the point budget; those
tables are now fetched with every build. This second round looks for what is still
missing for the stat table:

  * which race and class combinations exist
  * racial passives (spells granted by race)
  * level requirements on spells, and which skill lines grant which spells

Base stats by class, race and level are NOT probed: I cannot name a DB2 table that
holds them, and guessing names would only produce noise. Those probably have to come
from outside the client data (see the notes in the project plan).

Table names are educated guesses from the retail DB2 schema. A table Wago does not
export answers HTTP 400; that is useful to know too.

Run from the repo root (with PYTHONPATH=src):

    python3 scripts/probe_tables.py
    python3 scripts/probe_tables.py --version 1.60.1.70450
    python3 scripts/probe_tables.py --group per-level     # only the per-level stat rate tables

Writes nothing under data/ except data/probe/<build>-tables.txt, a text sample of
every table found. Upload that file; it is not part of any dataset.
"""

import argparse
import csv
import io
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wowcalc import transport  # noqa: E402
from wowcalc.fetch import WAGO_TABLE_URL, validate_csv  # noqa: E402

CANDIDATES = {
    "race and class combinations": [
        "CharBaseInfo", "CharStartOutfit", "ChrRaceXChrModel", "ChrCustomizationReq",
    ],
    "racial passives and spell granting": [
        "SkillLine", "SkillLineAbility", "SpellLevels", "SpellLearnSpell", "SpellMisc",
    ],
    # Round 3: stat-to-figure rates at EVERY level (the character panel gave level 1 and 60 only).
    # foreverdb says its crit/Intellect numbers come from PlayerExpectedStat. The Gt* names are the
    # classic game tables (crit per Agility/Intellect by class and level, base health and mana).
    "per-level stat rates": [
        "PlayerExpectedStat", "ExpectedStat", "ExpectedStatMod",
        "GtChanceToMeleeCrit", "GtChanceToMeleeCritBase", "GtChanceToSpellCrit", "GtChanceToSpellCritBase",
        "GtOCTBaseHPByClass", "GtOCTBaseMPByClass", "GtRegenHPPerSpt", "GtRegenMPPerSpt", "GtCombatRatings",
    ],
}
# Groups whose tables are small enough to keep whole (written as a CSV next to the sample).
FULL_GROUPS = {"per-level stat rates"}


def probe(table: str, version: str) -> dict:
    url = WAGO_TABLE_URL.format(table=table, version=version)
    try:
        resp = transport.get(url, timeout=60.0, retries=1)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        return {"table": table, "status": f"not exported (HTTP {code})"}
    except OSError as exc:
        return {"table": table, "status": f"network error: {exc}"}
    try:
        facts = validate_csv(resp.body)
    except ValueError as exc:
        return {"table": table, "status": f"unusable: {exc}"}
    text = resp.body.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    return {"table": table, "status": "ok", "rows": facts["rows"], "columns": facts["columns"], "sample": rows[:61], "text": text}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", default="1.60.1.70450")
    ap.add_argument("--group", action="append", metavar="WORDS",
                    help="only the groups whose name contains these words, e.g. --group per-level (repeatable)")
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[1]

    found = []
    print(f"Probing Wago for build {args.version}\n")
    chosen = {g: t for g, t in CANDIDATES.items() if not args.group or any(w.lower() in g.lower() for w in args.group)}
    if not chosen:
        print(f"No group matches {args.group}. Groups: {', '.join(CANDIDATES)}")
        return 2
    for group, tables in chosen.items():
        print(f"== {group}")
        for table in tables:
            r = probe(table, args.version)
            if r["status"] == "ok":
                cols = r["columns"]
                shown = ", ".join(cols[:14]) + (f", ... (+{len(cols) - 14})" if len(cols) > 14 else "")
                print(f"  {table:28} {r['rows']:>6,} rows   {shown}")
                found.append(r)
                if group in FULL_GROUPS:
                    whole = root / "data" / "probe" / f"{args.version}-{table}.csv"
                    whole.parent.mkdir(parents=True, exist_ok=True)
                    whole.write_text(r["text"], encoding="utf-8")
                    print(f"  {'':28} whole table saved to {whole}")
            else:
                print(f"  {table:28} {r['status']}")
        print()

    suffix = "-" + "-".join(w.lower() for w in args.group) if args.group else ""
    out = root / "data" / "probe" / f"{args.version}-tables{suffix}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for r in found:
            handle.write(f"### {r['table']}  ({r['rows']} rows)\n")
            for row in r["sample"]:
                handle.write("\t".join(row) + "\n")
            if r["rows"] > 60:
                handle.write(f"... ({r['rows'] - 60} more rows)\n")
            handle.write("\n")
    print(f"{len(found)} table(s) found. Sample written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
