"""Base stats by class, race and level.

This is an EXTERNAL data set: the game's client tables do not hold it (searched:
see the project notes). It was compiled by hand from sixtyupgrades.com for levels
1 and 60 and lives in data/external/base_stats.tsv, tab separated:

    Class  Race  Health - level 1  Mana-L1  Strength-L1 ... Spirit-L1  Health - level 60 ...

Header names are matched loosely ("Health – level 1", "Mana–L1" and "Strength-L1"
are all fine), and any set of levels is accepted, so more levels can be added as
columns later without changing code. A blank mana cell means the class has no mana.

What this module guarantees, and what it does not:

  * Values at the supplied levels are returned as given (exact=True).
  * Values between them are LINEAR ESTIMATES (exact=False). Real stat growth is not
    exactly linear, so an estimate can be off by a point or two, and health and
    mana (which follow stamina and intellect) can be off by more. Supply more
    levels to tighten this.
  * The numbers appear to include racial percentage bonuses already (Human Spirit
    and Gnome Intellect gain more than other races in every class). If so, racial
    percentages must NOT be applied again on top. Unconfirmed; see the project notes.
  * Nothing here is verified against the game. `outliers` lists cells worth
    re-checking at the source, and `map_to_game` checks the combinations and names
    against the game's own tables.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .dataset import Dataset, DatasetError

STATS = ("health", "mana", "strength", "agility", "stamina", "intellect", "spirit")
PRIMARY = ("strength", "agility", "stamina")  # checked for outliers; intellect/spirit carry racial bonuses
_HEADER = re.compile(
    r"^(health|mana|strength|agility|stamina|intellect|spirit)\s*[\u2013\u2014-]?\s*(?:level|l)\s*(\d+)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StatValue:
    value: int | None  # None: the class has no such stat (mana for Warriors and Rogues)
    exact: bool  # True only at a level the source supplied


@dataclass(frozen=True)
class Row:
    class_name: str
    race_name: str
    points: dict[str, dict[int, int | None]]  # stat -> level -> value


@dataclass
class BaseStatTable:
    rows: dict[tuple[str, str], Row] = field(default_factory=dict)
    levels: tuple[int, ...] = ()
    problems: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def classes(self) -> list[str]:
        return sorted({c for c, _ in self.rows})

    @property
    def races(self) -> list[str]:
        return sorted({r for _, r in self.rows})

    def combos(self) -> list[tuple[str, str]]:
        return sorted(self.rows)

    def at(self, class_name: str, race_name: str, level: int) -> dict[str, StatValue]:
        """All stats for a character of `level`. Raises KeyError for a combination
        the table does not have, ValueError for a level outside the supplied range."""
        row = self.rows[(class_name, race_name)]
        if not self.levels or not (self.levels[0] <= level <= self.levels[-1]):
            raise ValueError(f"level {level} is outside the supplied range {self.levels[:1]}..{self.levels[-1:]}")
        out: dict[str, StatValue] = {}
        for stat in STATS:
            series = row.points[stat]
            if level in series:
                out[stat] = StatValue(series[level], True)
                continue
            lo = max(lv for lv in self.levels if lv < level)
            hi = min(lv for lv in self.levels if lv > level)
            a, b = series[lo], series[hi]
            if a is None or b is None:
                out[stat] = StatValue(None, False)
            else:
                out[stat] = StatValue(math.floor(a + (b - a) * (level - lo) / (hi - lo) + 0.5), False)
        return out

    def outliers(self, tolerance: int = 4) -> list[str]:
        """Cells worth re-checking at the source.

        Within a class, every race should gain about the same amount of strength,
        agility and stamina between the first and last level. A race that differs
        from the class's typical gain by more than `tolerance` is listed. Intellect
        and spirit are not checked because racial bonuses legitimately move them.
        This finds likely typos; it cannot prove a value right or wrong."""
        if len(self.levels) < 2:
            return []
        first, last = self.levels[0], self.levels[-1]
        notes: list[str] = []
        by_class: dict[str, list[Row]] = defaultdict(list)
        for row in self.rows.values():
            by_class[row.class_name].append(row)
        for class_name, rows in sorted(by_class.items()):
            if len(rows) < 3:
                continue
            for stat in PRIMARY:
                gains = {r.race_name: r.points[stat][last] - r.points[stat][first]
                         for r in rows if r.points[stat][first] is not None and r.points[stat][last] is not None}
                if len(gains) < 3:
                    continue
                typical = statistics.median(gains.values())
                for race, gain in sorted(gains.items()):
                    if abs(gain - typical) > tolerance:
                        row = self.rows[(class_name, race)]
                        notes.append(
                            f"{class_name}/{race} {stat}: gains {gain:+d} from level {first} to {last} "
                            f"({row.points[stat][first]} -> {row.points[stat][last]}), but the typical "
                            f"{class_name} gains {typical:+g}; about {row.points[stat][first] + typical:g} "
                            f"at level {last} would match")
        return notes


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def parse_base_stats(text: str, meta: dict | None = None) -> BaseStatTable:
    table = BaseStatTable(meta=meta or {})
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff"), newline=""), delimiter="\t")
    records = [r for r in reader if any(c.strip() for c in r)]
    if not records:
        table.problems.append("the file is empty")
        return table

    header = [h.strip() for h in records[0]]
    if len(header) < 2 or header[0].lower() != "class" or header[1].lower() != "race":
        table.problems.append("the first two columns must be 'Class' and 'Race'")
        return table

    columns: dict[int, tuple[str, int]] = {}
    for i, h in enumerate(header[2:], start=2):
        m = _HEADER.match(h)
        if not m:
            table.problems.append(f"unrecognised column header {h!r}")
            continue
        columns[i] = (m.group(1).lower(), int(m.group(2)))
    levels = sorted({lv for _, lv in columns.values()})
    table.levels = tuple(levels)
    present = {(stat, lv) for stat, lv in columns.values()}
    for stat in STATS:
        for lv in levels:
            if (stat, lv) not in present:
                table.problems.append(f"no column for {stat} at level {lv}")
    if table.problems:
        return table

    for line_no, record in enumerate(records[1:], start=2):
        if len(record) != len(header):
            table.problems.append(f"line {line_no}: {len(record)} fields, expected {len(header)}")
            continue
        class_name, race_name = record[0].strip(), record[1].strip()
        if not class_name or not race_name:
            table.problems.append(f"line {line_no}: class or race is empty")
            continue
        if (class_name, race_name) in table.rows:
            table.problems.append(f"line {line_no}: duplicate row for {class_name}/{race_name}")
            continue
        points: dict[str, dict[int, int | None]] = {s: {} for s in STATS}
        ok = True
        for i, (stat, lv) in columns.items():
            cell = record[i].strip()
            if cell == "":
                points[stat][lv] = None
                continue
            try:
                value = int(cell)
            except ValueError:
                table.problems.append(f"line {line_no} ({class_name}/{race_name}): {stat} at level {lv} is not a whole number: {cell!r}")
                ok = False
                continue
            if value < 0:
                table.problems.append(f"line {line_no} ({class_name}/{race_name}): {stat} at level {lv} is negative")
                ok = False
            points[stat][lv] = value
        if not ok:
            continue
        table.rows[(class_name, race_name)] = Row(class_name, race_name, points)

    _check_rows(table)
    return table


def _check_rows(table: BaseStatTable) -> None:
    mana_blank: dict[str, set[bool]] = defaultdict(set)
    for (class_name, race_name), row in table.rows.items():
        label = f"{class_name}/{race_name}"
        for stat in STATS:
            values = [row.points[stat][lv] for lv in table.levels]
            if stat == "mana":
                if any(v is None for v in values) and not all(v is None for v in values):
                    table.problems.append(f"{label}: mana is blank at some levels but not others")
                mana_blank[class_name].add(all(v is None for v in values))
                continue
            if any(v is None for v in values):
                table.problems.append(f"{label}: {stat} is blank")
                continue
            if any(b < a for a, b in zip(values, values[1:])):
                table.problems.append(f"{label}: {stat} goes down as level rises ({values})")
    for class_name, states in sorted(mana_blank.items()):
        if len(states) > 1:
            table.problems.append(f"{class_name}: some races have mana and some do not")


def load_base_stats(path: Path) -> BaseStatTable:
    path = Path(path)
    meta_path = path.with_name(path.stem + ".meta.json")
    meta = {}
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return parse_base_stats(path.read_text(encoding="utf-8"), meta)


# --------------------------------------------------------------------------
# Checking against the game's own tables
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class GameMapping:
    class_ids: dict[str, int]
    race_ids: dict[str, int]


def map_to_game(table: BaseStatTable, ds: Dataset) -> tuple[GameMapping, list[str]]:
    """Tie the file's class and race names to the game's IDs, and compare the
    file's race/class combinations with the game's own list (CharBaseInfo).

    Races are matched by name among the races that actually appear in CharBaseInfo
    (the race table also holds NPC and mount forms with the same names). The two
    Skyborne variants are told apart by which classes each can be."""
    problems: list[str] = []
    class_ids = {r["Name_lang"]: int(r["ID"]) for r in ds.rows("ChrClasses") if r.get("ID")}
    race_names = {int(r["ID"]): r["Name_lang"] for r in ds.rows("ChrRaces") if r.get("ID")}
    game: dict[int, set[int]] = defaultdict(set)  # race id -> class ids
    for r in ds.rows("CharBaseInfo"):
        game[int(r["RaceID"])].add(int(r["ClassID"]))

    mapped_classes: dict[str, int] = {}
    for name in table.classes:
        if name in class_ids:
            mapped_classes[name] = class_ids[name]
        else:
            problems.append(f"class {name!r} is not a class in the game data")

    file_classes: dict[str, set[str]] = defaultdict(set)
    for class_name, race_name in table.rows:
        file_classes[race_name].add(class_name)

    by_name: dict[str, list[int]] = defaultdict(list)
    for rid in game:
        by_name[race_names.get(rid, "").lower()].append(rid)
    skyborne = [rid for rid in game if "skyborne" in race_names.get(rid, "").lower()]

    mapped_races: dict[str, int] = {}
    for race in table.races:
        if race.lower().startswith("skyborne"):
            wanted = {mapped_classes[c] for c in file_classes[race] if c in mapped_classes}
            matches = [rid for rid in skyborne if game[rid] == wanted]
            if len(matches) == 1:
                mapped_races[race] = matches[0]
            elif not matches:
                problems.append(f"race {race!r}: no Skyborne race in the game data has this class set")
            else:
                problems.append(f"race {race!r}: more than one Skyborne race has this class set; cannot tell which it is")
            continue
        ids = by_name.get(race.lower(), [])
        if len(ids) == 1:
            mapped_races[race] = ids[0]
        else:
            problems.append(f"race {race!r}: {'not found' if not ids else 'ambiguous'} among the races the game lets you create")
    taken = list(mapped_races.values())
    if len(set(taken)) != len(taken):
        problems.append("two race names in the file map to the same game race")

    file_pairs = {(mapped_races[r], mapped_classes[c]) for c, r in table.rows if r in mapped_races and c in mapped_classes}
    game_pairs = {(rid, cid) for rid, cids in game.items() for cid in cids}
    for rid, cid in sorted(game_pairs - file_pairs):
        problems.append(f"the game allows {race_names.get(rid, rid)}/{_class_name(class_ids, cid)} but the file has no row for it")
    for rid, cid in sorted(file_pairs - game_pairs):
        problems.append(f"the file has {race_names.get(rid, rid)}/{_class_name(class_ids, cid)}, which the game does not allow")
    return GameMapping(mapped_classes, mapped_races), problems


def _class_name(class_ids: dict[str, int], cid: int) -> str:
    return next((n for n, i in class_ids.items() if i == cid), str(cid))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    from .fetch import TABLES
    from .update import UpdateError, get_active

    repo_root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description="Check the base stats file.")
    ap.add_argument("--file", type=Path, help="default: <data-dir>/external/base_stats.tsv")
    ap.add_argument("--build", help="build to cross-check against (default: the active build)")
    ap.add_argument("--data-dir", type=Path, default=repo_root / "data")
    args = ap.parse_args(argv)

    path = args.file or args.data_dir / "external" / "base_stats.tsv"
    if not path.is_file():
        print(f"No base stats file at {path}")
        return 2
    table = load_base_stats(path)
    print(f"{path.name}: {len(table.rows)} combinations, {len(table.classes)} classes, "
          f"{len(table.races)} races, levels {', '.join(map(str, table.levels)) or '(none)'}")
    if table.meta:
        print(f"source: {table.meta.get('source', '?')} | verification: {table.meta.get('verification', '?')}")

    failed = bool(table.problems)
    if table.problems:
        print(f"\n{len(table.problems)} problem(s) in the file:")
        for p in table.problems:
            print(f"  - {p}")
    notes = table.outliers() if not table.problems else []
    if notes:
        print(f"\n{len(notes)} value(s) worth re-checking at the source:")
        for n in notes:
            print(f"  - {n}")
    elif not table.problems:
        print("\nNo outliers.")

    if not table.problems:
        try:
            build = args.build or get_active(args.data_dir)
        except UpdateError as exc:
            print(f"\nGame cross-check skipped: {exc}")
            build = None
        if build:
            raw = args.data_dir / "raw" / build
            missing = [t for t in TABLES if not (raw / f"{t}.csv").is_file()]
            if missing:
                print(f"\nGame cross-check skipped: build {build} lacks {', '.join(missing[:3])}"
                      f"{'...' if len(missing) > 3 else ''}. Run: python -m wowcalc.update complete")
            else:
                try:
                    mapping, problems = map_to_game(table, Dataset(raw, build))
                except DatasetError as exc:
                    print(f"\nGame cross-check failed: {exc}")
                    return 2
                if problems:
                    failed = True
                    print(f"\nCross-check against build {build}: {len(problems)} problem(s):")
                    for p in problems:
                        print(f"  - {p}")
                else:
                    print(f"\nCross-check against build {build}: all {len(table.rows)} combinations match the game's list.")
                    print("  race IDs: " + ", ".join(f"{r}={i}" for r, i in sorted(mapping.race_ids.items(), key=lambda kv: kv[1])))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
