"""Per-level stat rates from the game client's own PlayerExpectedStat table.

The character panel (sheet_base.tsv) exists only at levels 1 and 60. This table, exported
by the probe script (`scripts/probe_tables.py --group per-level`) and kept as
`data/external/PlayerExpectedStat.csv`, holds for every class and level:

    CritPerAgility          fraction of a crit % point per Agility (melee crit AND dodge)
    SpellCritPerIntellect   the same for Intellect
    BaseMana                the class's mana before Intellect
    HPPerStamina            10

What was checked against the panel (build 1.60.1.70450, levels 1 and 60, every class/race):

  * crit and dodge:  panel figure = class constant + Agility x rate(level). The constant is the
                     same at level 1 and level 60, so it is taken as level-independent (an
                     inference for the levels between, labelled as such wherever it is used).
  * spell crit:      the same, plus Intellect x rate(level).
  * mana:            floor((BaseMana + min(Intellect, 20) + 15 x max(Intellect - 20, 0)) x race factor)
                     reproduces 73 of 74 panel rows; the 74th (Mage / Orc, level 1) looks like a typo.
  * attack power:    the per-Strength and per-Agility points are the same at every level, but the
                     constant grows with level (-17 at level 1, +160 at level 60 for a Paladin).
                     When it grows by a whole number of points per level it is extended in a
                     straight line; otherwise the class has no attack power between the two levels.

The client stores each rate to about four to six significant digits (0.001667 for 1/600), so rates
are read to six significant digits and the result is exact fractions, never floats.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

CLASS_IDS = {1: "Warrior", 2: "Paladin", 3: "Hunter", 4: "Rogue", 5: "Priest", 7: "Shaman", 8: "Mage", 9: "Warlock", 11: "Druid"}
REQUIRED = ("Level", "ClassID", "ContentSetID", "BaseMana", "HPPerStamina", "CritPerAgility", "SpellCritPerIntellect")
TOL = Fraction(5, 1000)  # the panel shows two decimals
FREE_INTELLECT = 20      # the first 20 Intellect give 1 mana each, the rest give 15


class ExpectedError(ValueError):
    """The PlayerExpectedStat file cannot be used."""


def parse_rate(text: str) -> Fraction:
    """A table value (crit fraction per stat point) as percent per stat point, at 6 significant digits."""
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ExpectedError(f"not a number: {text!r}") from exc
    if value < 0:
        raise ExpectedError(f"negative rate: {text!r}")
    if value == 0:
        return Fraction(0)
    return Fraction(value.quantize(Decimal(1).scaleb(value.adjusted() - 5))) * 100


@dataclass(frozen=True)
class LevelRate:
    agility_pct: Fraction            # percent of crit/dodge per Agility
    intellect_pct: Fraction | None   # percent of spell crit per Intellect; None: the class has no spell crit
    base_mana: int
    hp_per_stamina: int


def mana_total(base_mana: int, intellect: int, factor: Fraction = Fraction(1)) -> int:
    """BaseMana + 1 per Intellect up to 20 + 15 per Intellect beyond, times the race factor, rounded down."""
    points = min(intellect, FREE_INTELLECT) + 15 * max(intellect - FREE_INTELLECT, 0)
    return math.floor((base_mana + points) * factor)


def load_expected(path: Path) -> dict[str, dict[int, LevelRate]]:
    """class name -> level -> rates. Rows for classes this project does not model are ignored."""
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
            if missing:
                raise ExpectedError(f"{path.name}: missing columns {', '.join(missing)}")
            out: dict[str, dict[int, LevelRate]] = {}
            for n, row in enumerate(reader, start=2):
                if row["ContentSetID"].strip() not in ("", "0"):
                    continue
                try:
                    cls = CLASS_IDS.get(int(row["ClassID"]))
                    level = int(row["Level"])
                    if cls is None:
                        continue
                    spell = parse_rate(row["SpellCritPerIntellect"])
                    rate = LevelRate(parse_rate(row["CritPerAgility"]), spell if spell else None,
                                     int(row["BaseMana"]), int(row["HPPerStamina"]))
                except (ValueError, ExpectedError) as exc:
                    raise ExpectedError(f"{path.name} line {n}: {exc}") from exc
                if level in out.setdefault(cls, {}):
                    raise ExpectedError(f"{path.name} line {n}: {cls} level {level} appears twice")
                out[cls][level] = rate
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ExpectedError(f"cannot read {path}: {exc}") from exc
    if not out:
        raise ExpectedError(f"{path.name}: no usable rows")
    return out


Range = tuple[Fraction, Fraction]


def _intersect(a: Range | None, lo: Fraction, hi: Fraction) -> Range | None:
    if a is None:
        return (lo, hi)
    lo, hi = max(a[0], lo), min(a[1], hi)
    return (lo, hi) if lo <= hi else None


@dataclass
class ClientModel:
    """The per-level rates plus the class constants that tie them to the panel."""
    rates: dict[str, dict[int, LevelRate]] = field(default_factory=dict)
    crit_const: dict[str, Range] = field(default_factory=dict)    # percent, melee crit at 0 Agility
    dodge_const: dict[str, Range] = field(default_factory=dict)   # percent, before racial bonuses
    spell_const: dict[str, Range] = field(default_factory=dict)   # percent, spell crit at 0 Agility and 0 Intellect
    ap_constants: dict[str, tuple[int, int]] = field(default_factory=dict)  # attack power constant at the two anchor levels
    anchors: tuple[int, int] = (1, 60)

    def rate(self, class_name: str, level: int) -> LevelRate | None:
        return self.rates.get(class_name, {}).get(level)

    def ap_constant(self, class_name: str, level: int) -> int | None:
        """The attack power constant at `level`: exact at the anchor levels, a straight line between and beyond
        them when it climbs by whole points per level, otherwise None."""
        pair = self.ap_constants.get(class_name)
        if pair is None:
            return None
        lo, hi = self.anchors
        step = Fraction(pair[1] - pair[0], hi - lo)
        if step.denominator != 1:
            return None if level not in self.anchors else pair[self.anchors.index(level)]
        return pair[0] + int(step) * (level - lo)

    # ---- file form ----
    def to_json(self) -> dict:
        def rng(d):
            return {c: [str(a), str(b)] for c, (a, b) in sorted(d.items())}
        return {
            "note": "Per-level rates from the client's PlayerExpectedStat table (percent per stat point), with the class constants that tie them to the panel.",
            "anchors": list(self.anchors),
            "rates": {c: {str(l): [str(r.agility_pct), None if r.intellect_pct is None else str(r.intellect_pct), r.base_mana, r.hp_per_stamina]
                          for l, r in sorted(levels.items())} for c, levels in sorted(self.rates.items())},
            "crit_const": rng(self.crit_const), "dodge_const": rng(self.dodge_const), "spell_const": rng(self.spell_const),
            "ap_constants": {c: list(v) for c, v in sorted(self.ap_constants.items())},
        }

    @classmethod
    def from_json(cls, data: dict) -> "ClientModel":
        def rng(d):
            return {c: (Fraction(a), Fraction(b)) for c, (a, b) in d.items()}
        return cls(
            rates={c: {int(l): LevelRate(Fraction(a), None if i is None else Fraction(i), int(m), int(h))
                       for l, (a, i, m, h) in levels.items()} for c, levels in data["rates"].items()},
            crit_const=rng(data["crit_const"]), dodge_const=rng(data["dodge_const"]), spell_const=rng(data["spell_const"]),
            ap_constants={c: (int(a), int(b)) for c, (a, b) in data["ap_constants"].items()},
            anchors=(int(data["anchors"][0]), int(data["anchors"][1])))


def fit_client(expected, rows_by_level: dict[int, list[dict]], racial_dodge: dict[str, Fraction],
               ap_rules: dict, problems: list[str], mana_factors: dict[str, Fraction] | None = None) -> ClientModel:
    """Tie the client's rates to the panel and report every disagreement.

    `rows_by_level`: level -> rows as built by rates._combos_at (class, race, strength, agility, intellect,
    mana, mc, sc, dodge, ap). `ap_rules`: the attack power rule per class fitted at the main level.
    """
    model = ClientModel(rates=expected)
    levels = sorted(rows_by_level)
    model.anchors = (levels[0], levels[-1]) if len(levels) >= 2 else (levels[0], levels[0])
    mana_factors = mana_factors or {}
    classes = sorted({r["class"] for rows in rows_by_level.values() for r in rows})
    for cls in classes:
        if cls not in expected:
            problems.append(f"{cls}: not in the PlayerExpectedStat table")
            continue
        crit = dodge = spell = None
        ap_const: dict[int, set[int]] = {}
        bad_ap: list[tuple[int, dict, int]] = []
        for lv in levels:
            rate = expected[cls].get(lv)
            if rate is None:
                problems.append(f"{cls}: no PlayerExpectedStat row for level {lv}")
                continue
            for r in (r for r in rows_by_level[lv] if r["class"] == cls):
                agi = Fraction(r["agility"])
                part = agi * rate.agility_pct
                if r["mc"] is not None:
                    crit = _intersect(crit, r["mc"] - TOL - part, r["mc"] + TOL - part)
                    if crit is None:
                        problems.append(f"{cls}: the table's crit rate does not fit {r['race']} at level {lv} together with the other races")
                        crit = (Fraction(0), Fraction(0))  # keep going, but the problem is recorded
                if r["dodge"] is not None:
                    racial = racial_dodge.get(r["race"], Fraction(0))
                    dodge = _intersect(dodge, r["dodge"] - racial - TOL - part, r["dodge"] - racial + TOL - part)
                    if dodge is None:
                        problems.append(f"{cls}: the table's rate does not fit {r['race']}'s dodge at level {lv} together with the other races")
                        dodge = (Fraction(0), Fraction(0))
                if r["sc"] is not None and rate.intellect_pct is not None:
                    both = part + Fraction(r["intellect"]) * rate.intellect_pct
                    spell = _intersect(spell, r["sc"] - TOL - both, r["sc"] + TOL - both)
                    if spell is None:
                        problems.append(f"{cls}: the table's spell crit rate does not fit {r['race']} at level {lv} together with the other races")
                        spell = (Fraction(0), Fraction(0))
                rule = ap_rules.get(cls)
                if rule is not None and r["ap"] is not None and Fraction(r["ap"]).denominator == 1:
                    k = int(r["ap"]) - rule.per_strength * r["strength"] - rule.per_agility * r["agility"]
                    ap_const.setdefault(lv, set()).add(k)
                    bad_ap.append((lv, r, k))
                if rate.base_mana and r["mana"] is not None:
                    expect = mana_total(rate.base_mana, int(r["intellect"]), mana_factors.get(r["race"], Fraction(1)))
                    if expect != int(r["mana"]):
                        problems.append(f"{r['race']} {cls} (level {lv}): mana {r['mana']} but the client table gives {expect} for Intellect {r['intellect']}; one of the two base figures may be a typo")
        if crit is not None:
            model.crit_const[cls] = crit
        if dodge is not None:
            model.dodge_const[cls] = dodge
        if spell is not None:
            model.spell_const[cls] = spell
        # attack power: the most common constant at each anchor level; rows that disagree are named
        picks = []
        for lv in (model.anchors[0], model.anchors[1]):
            counts: dict[int, int] = {}
            for l, r, k in bad_ap:
                if l == lv:
                    counts[k] = counts.get(k, 0) + 1
            if counts:
                best = max(sorted(counts), key=lambda k: counts[k])
                picks.append(best)
                rule = ap_rules[cls]
                for l, r, k in bad_ap:
                    if l == lv and k != best:
                        hint = ""
                        if rule.per_strength:
                            implied, rest = divmod(int(r["ap"]) - best - rule.per_agility * r["agility"], rule.per_strength)
                            hint = f" (attack power suggests Strength {implied})" if rest == 0 else ""
                        problems.append(f"{r['race']} {cls} (level {lv}): attack power {r['ap']} does not follow the class rule; "
                                        f"the base Strength {r['strength']} may be wrong{hint}")
        if len(picks) == 2:
            model.ap_constants[cls] = (picks[0], picks[1])
    return model
