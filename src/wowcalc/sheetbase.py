"""Base character-sheet figures (crit, hit, dodge, parry, attack power, armor ...) per
class, race and level, read from the in-game character panel.

These are hand-collected, like the base stats (basestats.py): a naked character at
level 1 and level 60, copied from the game's panels. The raw export is one wide row per
section with one column per class/race/level, each cell the panel's text run together
("Attack Power276Hit Chance0.00%Crit Chance4.32%..."). `convert` turns it into a tidy
long file, `data/external/sheet_base.tsv`:

    class  race  level  section  label  value  unit

Only levels 1 and 60 exist. Other levels are REFUSED, not estimated: crit and dodge
depend on agility and intellect at that level, and defense and armor change with it.

A figure that is absent for a combination means the panel did not show it, and is
reported as absent, not as zero (a Shaman at level 60 shows no Parry line).

    python -m wowcalc.sheetbase convert RAW_FILE [--out data/external/sheet_base.tsv]
    python -m wowcalc.sheetbase check [--file ...] [--base-stats ...]
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

CLASSES = ("Warrior", "Paladin", "Hunter", "Rogue", "Priest", "Shaman", "Mage", "Warlock", "Druid")
SECTIONS = ("spell", "melee", "defense", "ranged")
# Panel sections are rows of the raw export: row index -> section name.
RAW_SECTION_ROWS = {2: "spell", 4: "melee", 6: "defense", 8: "ranged"}
LEVELS = (1, 60)

HEADER = ["class", "race", "level", "section", "label", "value", "unit"]

_NAME = re.compile(r"^(?P<race>.+) (?P<cls>%s) (?P<level>\d+)$" % "|".join(CLASSES))
_PAIR = re.compile(r"([A-Za-z][A-Za-z .]*?)(-?\d[\d,]*(?:\.\d+)?%?)(?=[A-Za-z]|$)")
# The spell panel of Mages and Warlocks starts with a stray copy of the selected school.
_STRAY_SCHOOL = re.compile(r"^(?:Arcane|Fire|Frost|Holy|Nature|Shadow) Damage-?\d+(?=Spell Damage)")


class SheetBaseError(Exception):
    """The file is malformed or the request cannot be answered honestly."""


@dataclass(frozen=True)
class Figure:
    value: Decimal
    percent: bool


def parse_panel(cell: str) -> list[tuple[str, Decimal, bool]]:
    """One panel cell -> [(label, value, is_percent)], in the order shown."""
    cell = _STRAY_SCHOOL.sub("", cell.strip())
    cell = cell.replace("Mana per 5 Sec.", "Mana per Five Sec.")  # the 5 would read as a value
    pairs = _PAIR.findall(cell)
    if "".join(a + b for a, b in pairs) != cell:
        raise SheetBaseError(f"cannot read panel text: {cell[:80]!r}")
    out = []
    for label, text in pairs:
        percent = text.endswith("%")
        try:
            value = Decimal(text.rstrip("%").replace(",", ""))
        except InvalidOperation as exc:
            raise SheetBaseError(f"bad number {text!r} after {label!r}") from exc
        out.append((label.strip().replace("Five Sec.", "5 Sec."), value, percent))
    labels = [o[0] for o in out]
    if len(labels) != len(set(labels)):
        raise SheetBaseError(f"a label appears twice in one panel: {cell[:80]!r}")
    return out


def convert_raw(rows: list[list[str]]) -> list[list[str]]:
    """The raw wide export -> tidy rows (without the header)."""
    if len(rows) <= max(RAW_SECTION_ROWS):
        raise SheetBaseError(f"expected at least {max(RAW_SECTION_ROWS) + 1} rows, found {len(rows)}")
    out: list[list[str]] = []
    seen: set[tuple[str, str, int]] = set()
    for col, name in enumerate(rows[0]):
        if not name.strip():
            continue
        m = _NAME.match(name.strip())
        if not m:
            raise SheetBaseError(f"column {col + 1}: cannot read the heading {name!r} (expected 'Race Class Level')")
        key = (m["race"], m["cls"], int(m["level"]))
        if key in seen:
            raise SheetBaseError(f"column {col + 1}: {name!r} appears twice")
        seen.add(key)
        for row_index, section in RAW_SECTION_ROWS.items():
            cell = rows[row_index][col] if col < len(rows[row_index]) else ""
            if not cell.strip() or not re.search(r"\d", cell):
                continue  # a section this combination does not have (casters only, hunters only)
            for label, value, percent in parse_panel(cell):
                out.append([m["cls"], m["race"], m["level"], section, label, f"{value:f}", "%" if percent else ""])
    return out


class SheetBase:
    def __init__(self, rows: list[dict[str, str]]):
        self._figures: dict[tuple[str, str, int], dict[tuple[str, str], Figure]] = {}
        for r in rows:
            key = (r["class"], r["race"], int(r["level"]))
            slot = self._figures.setdefault(key, {})
            fig_key = (r["section"], r["label"])
            if fig_key in slot:
                raise SheetBaseError(f"{key}: {fig_key} appears twice")
            slot[fig_key] = Figure(Decimal(r["value"]), r["unit"] == "%")

    def combinations(self) -> set[tuple[str, str]]:
        return {(c, r) for c, r, _ in self._figures}

    def levels(self, class_name: str, race: str) -> list[int]:
        return sorted(l for c, r, l in self._figures if (c, r) == (class_name, race))

    def at(self, class_name: str, race: str, level: int) -> dict[tuple[str, str], Figure]:
        """All figures for the combination at `level`. KeyError for an unknown
        class/race; SheetBaseError for a level that was not recorded."""
        if (class_name, race) not in self.combinations():
            raise KeyError(f"no sheet figures for {race} {class_name}")
        if (class_name, race, level) not in self._figures:
            have = ", ".join(map(str, self.levels(class_name, race)))
            raise SheetBaseError(f"sheet figures exist only for level {have}, not {level}; they are not estimated")
        return self._figures[(class_name, race, level)]


def load_sheet_base(path: Path) -> SheetBase:
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, delimiter="\t")
            header = next(reader, None)
            if header != HEADER:
                raise SheetBaseError(f"{path}: expected the header {HEADER}, found {header}")
            rows = []
            for n, values in enumerate(reader, start=2):
                if not any(v.strip() for v in values):
                    continue
                if len(values) != len(HEADER):
                    raise SheetBaseError(f"{path} line {n}: expected {len(HEADER)} columns, found {len(values)}")
                rows.append(dict(zip(HEADER, values)))
    except (OSError, UnicodeError) as exc:
        raise SheetBaseError(f"cannot read {path}: {exc}") from exc
    try:
        return SheetBase(rows)
    except (ValueError, InvalidOperation) as exc:
        raise SheetBaseError(f"{path}: {exc}") from exc


def problems_in(base: SheetBase) -> list[str]:
    """Things worth a second look: missing levels and figures the panel should always show."""
    found = []
    for cls, race in sorted(base.combinations()):
        have = base.levels(cls, race)
        if have != list(LEVELS):
            found.append(f"{race} {cls}: levels {have}, expected {list(LEVELS)}")
        for level in have:
            figs = base.at(cls, race, level)
            for needed in (("melee", "Attack Power"), ("melee", "Crit Chance"), ("defense", "Dodge Chance"),
                           ("defense", "Armor"), ("defense", "Defense")):
                if needed not in figs:
                    found.append(f"{race} {cls} {level}: no {needed[0]} {needed[1]}")
    return found


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description="Convert or check the base character-sheet figures.")
    sub = ap.add_subparsers(dest="command", required=True)
    conv = sub.add_parser("convert", help="turn the raw wide export into the tidy file")
    conv.add_argument("raw", type=Path)
    conv.add_argument("--out", type=Path, default=repo_root / "data" / "external" / "sheet_base.tsv")
    chk = sub.add_parser("check", help="validate the tidy file")
    chk.add_argument("--file", type=Path, default=repo_root / "data" / "external" / "sheet_base.tsv")
    chk.add_argument("--base-stats", type=Path, default=repo_root / "data" / "external" / "base_stats.tsv")
    args = ap.parse_args(argv)

    if args.command == "convert":
        try:
            with args.raw.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.reader(handle, delimiter="\t"))
            tidy = convert_raw(rows)
        except (OSError, UnicodeError, SheetBaseError) as exc:
            print(f"ERROR: {exc}")
            return 2
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
            writer.writerow(HEADER)
            writer.writerows(tidy)
        print(f"wrote {len(tidy)} figures to {args.out}")
        return 0

    try:
        base = load_sheet_base(args.file)
    except SheetBaseError as exc:
        print(f"ERROR: {exc}")
        return 2
    combos = base.combinations()
    print(f"{args.file.name}: {len(combos)} class/race combinations, levels {sorted({l for _, _, l in base._figures})}")
    issues = problems_in(base)
    if args.base_stats.is_file():
        from .basestats import load_base_stats
        try:
            table = load_base_stats(args.base_stats)
        except Exception as exc:  # noqa: BLE001 - report, do not crash
            issues.append(f"cannot read {args.base_stats.name} to compare: {exc}")
        else:
            theirs = set(table.combos())
            for cls, race in sorted(theirs - combos):
                issues.append(f"{race} {cls} is in the base stats but has no sheet figures")
            for cls, race in sorted(combos - theirs):
                issues.append(f"{race} {cls} has sheet figures but no base stats")
    if issues:
        print(f"\n{len(issues)} thing(s) worth a look:")
        for line in issues:
            print(f"  - {line}")
        return 1
    print("no problems found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
