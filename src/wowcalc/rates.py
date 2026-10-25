"""How much crit, dodge and attack power a point of a stat is worth, fitted from the
game's own character panel.

The panel figures (sheet_base.tsv) and the base stats (base_stats.tsv) describe the same
56 class/race combinations, and races of one class differ in Agility, Intellect and
Strength. So the way each figure moves with each stat can be read off the data instead of
being remembered or guessed. Nothing here comes from outside knowledge; every rate is
FITTED and checked against every row.

What is fitted, at level 60 only (the only level whose rates are checked):

  agility -> melee crit and dodge   one rate per class, as a RANGE. The panel shows two
                                    decimals, so each rate is only known to an interval;
                                    the midpoint is used and the half-width reported.
  agility -> spell crit             assumed to be the same rate as melee crit, then CHECKED.
  intellect -> spell crit           one rate for every caster class (the intervals of all
                                    seven classes overlap, which is itself a check).
  attack power                      whole-number points per Strength and per Agility, per
                                    class. These fit with no rounding slack at all.

The rate ranges are exact: a rate is kept only if some constant makes every race in the
class round to the displayed value (see `slope_interval`).

    python -m wowcalc.rates fit      # writes data/external/stat_rates.json
    python -m wowcalc.rates show
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from pathlib import Path

from .basestats import BaseStatTable, load_base_stats
from .derived import ArmorRule, AssumedRules, PoolRule, fit_armor, fit_pool
from .perlevel import ClientModel, fit_client, load_expected, mana_total
from .sheetbase import Figure, SheetBase, load_sheet_base

LEVEL = 60
TOL = Fraction(5, 1000)  # the panel rounds to two decimals
MELEE_CRIT = ("melee", "Crit Chance")
SPELL_CRIT = ("spell", "Spell Crit")
DODGE = ("defense", "Dodge Chance")
ATTACK_POWER = ("melee", "Attack Power")
ARMOR = ("defense", "Armor")
DAMAGE_REDUCTION = ("defense", "Damage Reduction")
# Identical at levels 1 and 60 in every class/race row of the panel, so copied to other levels.
LEVEL_INDEPENDENT = (("melee", "Hit Chance"), ("spell", "Spell Hit"), ("defense", "Parry Chance"))


class RatesError(Exception):
    """The rates file is unreadable or cannot be used."""


@dataclass(frozen=True)
class Interval:
    """The range a rate (percent per stat point) is known to lie in."""
    lo: Fraction
    hi: Fraction

    @property
    def mid(self) -> Fraction:
        return (self.lo + self.hi) / 2

    @property
    def half_width(self) -> Fraction:
        return (self.hi - self.lo) / 2

    def __contains__(self, value) -> bool:
        return self.lo <= Fraction(value) <= self.hi

    def intersect(self, other: "Interval") -> "Interval | None":
        lo, hi = max(self.lo, other.lo), min(self.hi, other.hi)
        return Interval(lo, hi) if lo <= hi else None


def slope_interval(points: list[tuple[Fraction, Fraction]], tol: Fraction) -> Interval | None:
    """The slopes s for which some constant c has |c + s*x - y| <= tol at every point.

    For one input x this set is an interval, and a slope belongs to it exactly when every
    PAIR of points allows it: for x_i > x_j, (y_i - y_j - 2 tol) / (x_i - x_j) <= s <=
    (y_i - y_j + 2 tol) / (x_i - x_j). Returns None if no slope fits (infeasible) or if
    all the x values are equal (nothing to measure)."""
    lo: Fraction | None = None
    hi: Fraction | None = None
    for xi, yi in points:
        for xj, yj in points:
            if xi > xj:
                a, b = (yi - yj - 2 * tol) / (xi - xj), (yi - yj + 2 * tol) / (xi - xj)
                lo = a if lo is None else max(lo, a)
                hi = b if hi is None else min(hi, b)
    if lo is None or hi is None or lo > hi:
        return None
    return Interval(lo, hi)


@dataclass(frozen=True)
class AttackPowerRule:
    per_strength: int
    per_agility: int
    constant: int
    ambiguous: bool = False  # more than one whole-number rule fitted; the simplest was kept


@dataclass(frozen=True)
class StatEffects:
    """What a change in stats does to the panel. Percent points, or attack power."""
    melee_crit: Fraction
    spell_crit: Fraction | None  # None: the class has no spell crit rate
    dodge: Fraction
    attack_power: int

    def __add__(self, other: "StatEffects") -> "StatEffects":
        sc = None if self.spell_crit is None or other.spell_crit is None else self.spell_crit + other.spell_crit
        return StatEffects(self.melee_crit + other.melee_crit, sc, self.dodge + other.dodge,
                           self.attack_power + other.attack_power)


@dataclass
class RateTable:
    level: int = LEVEL
    agility: dict[str, Interval] = field(default_factory=dict)       # class -> percent per Agility
    intellect_spell_crit: Interval | None = None                      # percent per Intellect
    attack_power: dict[str, AttackPowerRule] = field(default_factory=dict)
    racial_dodge: dict[str, Fraction] = field(default_factory=dict)   # race -> flat dodge already in the panel
    problems: list[str] = field(default_factory=list)
    armor: ArmorRule | None = None
    health: PoolRule | None = None
    mana: PoolRule | None = None
    assumed: AssumedRules = field(default_factory=AssumedRules)
    # Optional point estimates from an outside source, kept only when they lie inside the measured range.
    agility_points: dict[str, Fraction] = field(default_factory=dict)
    intellect_points: dict[str, Fraction] = field(default_factory=dict)
    # Outside per-class attack power rates (Strength, Agility) that replace the panel fit for stat changes.
    attack_power_points: dict[str, tuple[int, int]] = field(default_factory=dict)
    references: dict = field(default_factory=dict)
    client: ClientModel | None = None   # the client's per-level rates, tied to the panel (perlevel.py)

    def covers(self, class_name: str, level: int) -> bool:
        """Can stat changes be turned into crit, dodge and attack power at this level?"""
        return level == self.level or (self.client is not None and self.client.rate(class_name, level) is not None)

    def effects(self, class_name: str, d_strength: int, d_agility: int, d_intellect: int,
                level: int | None = None) -> tuple[StatEffects, StatEffects]:
        """(best estimate, uncertainty) for stat changes. Raises KeyError for an unfitted class.
        With a `level` the client's exact per-level rates are used when the file has them."""
        agi = self.agility[class_name]
        rule = self.attack_power[class_name]
        ap_str, ap_agi = self.attack_power_points.get(class_name, (rule.per_strength, rule.per_agility))
        exact = None if self.client is None or level is None else self.client.rate(class_name, level)
        if exact is not None:
            a_rate, a_err = exact.agility_pct, Fraction(0)
            i_mid, i_err = exact.intellect_pct, Fraction(0)
            has_spell = i_mid is not None and class_name in self.spell_classes
        else:
            a_rate, a_err = self.agility_points.get(class_name, agi.mid), agi.half_width
            i_mid = None if self.intellect_spell_crit is None else self.intellect_points.get(class_name, self.intellect_spell_crit.mid)
            i_err = None if self.intellect_spell_crit is None else self.intellect_spell_crit.half_width
            has_spell = i_mid is not None and class_name in self.spell_classes
        best = StatEffects(
            a_rate * d_agility,
            a_rate * d_agility + i_mid * d_intellect if has_spell else None,
            a_rate * d_agility,
            ap_str * d_strength + ap_agi * d_agility)
        err = StatEffects(
            a_err * abs(d_agility),
            a_err * abs(d_agility) + i_err * abs(d_intellect) if has_spell else None,
            a_err * abs(d_agility), 0)
        return best, err

    def panel_estimate(self, class_name: str, race: str, level: int, strength: int, agility: int, intellect: int,
                       reference: dict | None = None) -> dict[tuple[str, str], Figure] | None:
        """Panel figures for a level the panel was not exported at, from the client's per-level rates and the
        class constants fitted at the two exported levels. Each value is within about 0.01 of the true one
        (the panel's rounding); attack power is exact when the class constant is known. `reference` is a real
        panel for the same character at an exported level, used for the figures that do not change with level."""
        if self.client is None:
            return None
        rate = self.client.rate(class_name, level)
        crit = self.client.crit_const.get(class_name)
        if rate is None or crit is None:
            return None

        def two(value: Fraction) -> Decimal:
            return (Decimal(value.numerator) / Decimal(value.denominator)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        mid = lambda pair: (pair[0] + pair[1]) / 2  # noqa: E731
        part = agility * rate.agility_pct
        figures = {MELEE_CRIT: Figure(two(mid(crit) + part), True)}
        dodge = self.client.dodge_const.get(class_name)
        if dodge is not None:
            figures[DODGE] = Figure(two(mid(dodge) + self.racial_dodge.get(race, Fraction(0)) + part), True)
        spell = self.client.spell_const.get(class_name)
        if spell is not None and rate.intellect_pct is not None:
            figures[SPELL_CRIT] = Figure(two(mid(spell) + part + intellect * rate.intellect_pct), True)
        ap_rule, ap_const = self.attack_power.get(class_name), self.client.ap_constant(class_name, level)
        if ap_rule is not None and ap_const is not None:
            figures[ATTACK_POWER] = Figure(Decimal(ap_rule.per_strength * strength + ap_rule.per_agility * agility + ap_const), False)
        if self.armor is not None:
            figures[ARMOR] = Figure(Decimal(self.armor.per_agility * agility), False)
        for key in LEVEL_INDEPENDENT:
            if reference and key in reference:
                figures[key] = reference[key]
        return figures

    spell_classes: frozenset = frozenset()

    # ---- file form ----
    def to_json(self) -> dict:
        def iv(i: Interval) -> list[str]:
            return [str(i.lo), str(i.hi)]
        return {
            "level": self.level,
            "note": "Fitted from sheet_base.tsv and base_stats.tsv by `python -m wowcalc.rates fit`. Rates are percent per stat point, as exact fractions.",
            "agility": {c: iv(i) for c, i in sorted(self.agility.items())},
            "intellect_spell_crit": None if self.intellect_spell_crit is None else iv(self.intellect_spell_crit),
            "spell_classes": sorted(self.spell_classes),
            "attack_power": {c: [r.per_strength, r.per_agility, r.constant, r.ambiguous] for c, r in sorted(self.attack_power.items())},
            "racial_dodge": {r: str(v) for r, v in sorted(self.racial_dodge.items())},
            "armor": None if self.armor is None else {"per_agility": self.armor.per_agility, "worst_dr_error": str(self.armor.worst_dr_error),
                                                       "formula": "armor / (armor + 85 x level + 400)"},
            "health": _pool_json(self.health),
            "mana": _pool_json(self.mana),
            "assumed": self.assumed.to_json(),
            "agility_points": {c: str(v) for c, v in sorted(self.agility_points.items())},
            "intellect_points": {c: str(v) for c, v in sorted(self.intellect_points.items())},
            "attack_power_points": {c: list(v) for c, v in sorted(self.attack_power_points.items())},
            "client": None if self.client is None else self.client.to_json(),
            "references": self.references,
            "problems": self.problems,
        }

    @classmethod
    def from_json(cls, data: dict) -> "RateTable":
        try:
            def iv(pair) -> Interval:
                return Interval(Fraction(pair[0]), Fraction(pair[1]))
            table = cls(
                level=int(data["level"]),
                agility={c: iv(p) for c, p in data["agility"].items()},
                intellect_spell_crit=None if data["intellect_spell_crit"] is None else iv(data["intellect_spell_crit"]),
                attack_power={c: AttackPowerRule(int(a), int(b), int(k), bool(amb)) for c, (a, b, k, amb) in data["attack_power"].items()},
                racial_dodge={r: Fraction(v) for r, v in data.get("racial_dodge", {}).items()},
                problems=list(data.get("problems", [])))
            table.spell_classes = frozenset(data.get("spell_classes", []))
            if data.get("armor"):
                table.armor = ArmorRule(int(data["armor"]["per_agility"]), Fraction(data["armor"]["worst_dr_error"]))
            table.health = _pool_from_json(data.get("health"))
            table.mana = _pool_from_json(data.get("mana"))
            if data.get("assumed"):
                table.assumed = AssumedRules.from_json(data["assumed"])
            table.agility_points = {c: Fraction(v) for c, v in data.get("agility_points", {}).items()}
            table.intellect_points = {c: Fraction(v) for c, v in data.get("intellect_points", {}).items()}
            table.attack_power_points = {c: (int(a), int(b)) for c, (a, b) in data.get("attack_power_points", {}).items()}
            table.references = dict(data.get("references", {}))
            table.client = ClientModel.from_json(data["client"]) if data.get("client") else None
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            raise RatesError(f"the rates file is not in the expected form: {exc!r}") from exc
        for c, i in table.agility.items():
            if i.lo > i.hi:
                raise RatesError(f"{c}: empty agility range")
        return table


def apply_references(table: RateTable, refs: dict) -> None:
    """Use an outside source's per-class rates as the point estimate, but ONLY where they lie inside the
    range the panel proves. A reference outside its range is a disagreement and is reported, not used.
    `refs` holds 'agility_per_crit' and 'intellect_per_spell_crit' (stat points per 1%) by class."""
    table.references = {k: v for k, v in refs.items() if k in ("source", "build", "extracted", "status")}
    per_str, per_agi = refs.get("attack_power_per_strength", {}), refs.get("attack_power_per_agility", {})
    for cls in sorted(set(per_str) | set(per_agi)):
        rule = table.attack_power.get(cls)
        if rule is None:
            continue
        points = (int(per_str.get(cls, rule.per_strength)), int(per_agi.get(cls, rule.per_agility)))
        if points != (rule.per_strength, rule.per_agility):
            # The reference replaces the panel fit for CHANGES in stats; the panel's own base figure is untouched.
            table.attack_power_points[cls] = points
            table.references.setdefault("attack_power_overrides", []).append(
                f"{cls}: panel fit {rule.per_strength} per Strength + {rule.per_agility} per Agility, "
                f"replaced by {points[0]} + {points[1]} (reference)")
    for cls, text in refs.get("agility_per_crit", {}).items():
        interval = table.agility.get(cls)
        if interval is None:
            continue
        rate = 1 / Fraction(str(text))
        if rate in interval:
            table.agility_points[cls] = rate
        else:
            table.problems.append(f"reference agility rate for {cls} ({text} per 1%) is outside what the panel allows "
                                  f"({float(1 / interval.hi):.1f} to {float(1 / interval.lo):.1f}); not used")
    iv = table.intellect_spell_crit
    for cls, text in refs.get("intellect_per_spell_crit", {}).items():
        if iv is None or cls not in table.spell_classes:
            continue
        rate = 1 / Fraction(str(text))
        if rate in iv:
            table.intellect_points[cls] = rate
        else:
            table.problems.append(f"reference intellect rate for {cls} ({text} per 1%) is outside what the panel allows "
                                  f"({float(1 / iv.hi):.1f} to {float(1 / iv.lo):.1f}); not used")


def _pool_json(rule: PoolRule | None):
    if rule is None:
        return None
    return {"per_point": rule.per_point, "constants": dict(sorted(rule.constants.items())),
            "race_factors": {r: str(f) for r, f in sorted(rule.race_factors.items())}}


def _pool_from_json(data):
    if not data:
        return None
    return PoolRule(int(data["per_point"]), {c: int(v) for c, v in data["constants"].items()},
                    {r: Fraction(v) for r, v in data["race_factors"].items()})


def load_rates(path: Path) -> RateTable:
    try:
        return RateTable.from_json(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RatesError(f"cannot read {path}: {exc}") from exc


# --------------------------------------------------------------------------
# Fitting
# --------------------------------------------------------------------------

def _combos_at(stats: BaseStatTable, panel: SheetBase, level: int) -> list[dict]:
    out = []
    for cls, race in stats.combos():
        base = stats.at(cls, race, level)
        figs = panel.at(cls, race, level)
        row = {"class": cls, "race": race, "health": base["health"].value, "mana": base["mana"].value}
        for stat in ("strength", "agility", "stamina", "intellect"):
            row[stat] = base[stat].value
        for name, key in (("mc", MELEE_CRIT), ("sc", SPELL_CRIT), ("dodge", DODGE), ("ap", ATTACK_POWER), ("armor", ARMOR)):
            row[name] = Fraction(str(figs[key].value)) if key in figs else None
        out.append(row)
    return out


def fit_rates(stats: BaseStatTable, panel: SheetBase, level: int = LEVEL, expected=None) -> RateTable:
    """Fit every rate from the two tables. Problems are recorded, never hidden."""
    table = RateTable(level=level)
    rows = _combos_at(stats, panel, level)
    if any(r[s] is None for r in rows for s in ("strength", "agility", "stamina", "intellect")):
        table.problems.append("some base stats are missing at this level")
        return table
    classes = sorted({r["class"] for r in rows})

    for cls in classes:
        mine = [r for r in rows if r["class"] == cls]
        # --- agility -> melee crit
        interval = slope_interval([(Fraction(r["agility"]), r["mc"]) for r in mine if r["mc"] is not None], TOL)
        if interval is None:
            table.problems.append(f"{cls}: no single agility rate explains melee crit across races")
        else:
            table.agility[cls] = interval
        # --- attack power
        rule = _fit_attack_power(mine)
        if rule is None:
            table.problems.append(f"{cls}: attack power is not a whole-number rule of Strength and Agility")
        else:
            table.attack_power[cls] = rule

    # --- racial dodge: dodge - melee crit differs between races only by flat bonuses
    diffs = {(r["class"], r["race"]): r["dodge"] - r["mc"] for r in rows if r["dodge"] is not None and r["mc"] is not None}
    by_class: dict[str, list[Fraction]] = {}
    for (cls, _), d in diffs.items():
        by_class.setdefault(cls, []).append(d)
    for cls, values in by_class.items():
        typical = Fraction(statistics.median(values)).limit_denominator(1000)
        spread = []
        for (c, race), d in diffs.items():
            if c != cls:
                continue
            bonus = d - typical
            whole = Fraction(round(bonus * 2), 2) if abs(bonus) >= Fraction(1, 2) else Fraction(0)
            if whole:
                previous = table.racial_dodge.get(race)
                if previous is not None and previous != whole:
                    table.problems.append(f"{race}: racial dodge differs between classes ({previous} vs {whole})")
                table.racial_dodge[race] = whole
            spread.append(bonus - whole)
        if max(spread) - min(spread) > 4 * TOL:
            table.problems.append(f"{cls}: dodge does not follow the melee crit rate once racial bonuses are removed")

    # --- intellect -> spell crit, one rate for every caster class
    casters = [c for c in classes if any(r["sc"] is not None for r in rows if r["class"] == c)]
    table.spell_classes = frozenset(casters)
    common: Interval | None = None
    ok = True
    for cls in casters:
        mine = [r for r in rows if r["class"] == cls and r["sc"] is not None and r["mc"] is not None]
        iv = slope_interval([(Fraction(r["intellect"]), r["sc"] - r["mc"]) for r in mine], 2 * TOL)
        if iv is None:
            table.problems.append(f"{cls}: no intellect rate explains spell crit (once the agility part is removed)")
            ok = False
            continue
        common = iv if common is None else common.intersect(iv)
        if common is None:
            table.problems.append("the classes do not share one intellect rate for spell crit")
            ok = False
            break
    if ok and common is not None:
        table.intellect_spell_crit = common

    # --- armor, damage reduction, health, mana
    dr_rows = []
    for lv in (1, level):
        for cls, race in stats.combos():
            figs = panel.at(cls, race, lv) if lv in panel.levels(cls, race) else {}
            if ARMOR in figs and DAMAGE_REDUCTION in figs:
                dr_rows.append((lv, Fraction(str(figs[ARMOR].value)), Fraction(str(figs[DAMAGE_REDUCTION].value))))
    table.armor = fit_armor(rows, dr_rows, table.problems)
    if table.armor is not None:  # armor is exactly k x Agility at every level, so it audits the other levels' Agility too
        for lv in sorted({l for c, r in stats.combos() for l in panel.levels(c, r)} - {level}):
            for cls, race in stats.combos():
                if lv not in panel.levels(cls, race) or ARMOR not in panel.at(cls, race, lv):
                    continue
                agility = stats.at(cls, race, lv)["agility"]
                armor = panel.at(cls, race, lv)[ARMOR].value
                if agility.exact and agility.value is not None and armor != table.armor.per_agility * agility.value:
                    table.problems.append(f"{race} {cls} (level {lv}): armor {armor} but {table.armor.per_agility} x Agility is "
                                          f"{table.armor.per_agility * agility.value}; the base stat Agility-L{lv} may be wrong (armor suggests {armor / table.armor.per_agility:g})")
    table.health = fit_pool(rows, "stamina", "health", table.problems, "health")
    table.mana = fit_pool(rows, "intellect", "mana", table.problems, "mana")
    if expected is not None:
        shared = sorted(set.intersection(*({l for l in panel.levels(c, r)} for c, r in stats.combos())))
        by_level = {lv: _combos_at(stats, panel, lv) for lv in shared}
        factors = {} if table.mana is None else table.mana.race_factors
        table.client = fit_client(expected, by_level, table.racial_dodge, table.attack_power, table.problems, factors)
    return table


def _fit_attack_power(mine: list[dict]) -> AttackPowerRule | None:
    """Whole-number (per Strength, per Agility) with one constant for the class."""
    pts = [(r["strength"], r["agility"], r["ap"]) for r in mine if r["ap"] is not None]
    if not pts:
        return None
    found = []
    for b in range(0, 4):
        for a in range(0, 4):
            constants = {p[2] - a * p[0] - b * p[1] for p in pts}
            if len(constants) == 1 and next(iter(constants)).denominator == 1:
                found.append((b, a, int(next(iter(constants)))))
    if not found:
        return None
    b, a, k = min(found)  # the rule with the least Agility, then the least Strength
    return AttackPowerRule(a, b, k, ambiguous=len(found) > 1)


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def describe(table: RateTable) -> list[str]:
    lines = [f"level {table.level} rates"]
    lines.append("  Agility per 1% crit and dodge (the panel's rounding allows the range shown):")
    for cls, iv in sorted(table.agility.items()):
        lines.append(f"    {cls:8} {float(1 / iv.hi):5.1f} .. {float(1 / iv.lo):5.1f}   (used: {float(1 / table.agility_points.get(cls, iv.mid)):.1f}{' - external reference' if cls in table.agility_points else ''})")
    iv = table.intellect_spell_crit
    if iv is not None:
        lines.append(f"  Intellect per 1% spell crit, every caster class: {float(1 / iv.hi):.1f} .. {float(1 / iv.lo):.1f}   (used: "
                     + (", ".join(f"{c[:4]} {float(1 / v):.1f}" for c, v in sorted(table.intellect_points.items())) or f"{float(1 / iv.mid):.1f}") + ")")
    lines.append("  Attack power:")
    for cls, r in sorted(table.attack_power.items()):
        lines.append(f"    {cls:8} {r.per_strength} per Strength" + (f" + {r.per_agility} per Agility" if r.per_agility else "")
                     + f" {r.constant:+d}" + ("   (more than one rule fits; simplest kept)" if r.ambiguous else ""))
    if table.armor is not None:
        lines.append(f"  Armor: {table.armor.per_agility} per Agility; damage reduction = armor / (armor + 85 x level + 400), worst gap to the panel {float(table.armor.worst_dr_error):.5f}")
    for label, rule in (("Health", table.health), ("Mana", table.mana)):
        if rule is not None:
            lines.append(f"  {label}: {rule.per_point} per {'Stamina' if label == 'Health' else 'Intellect'}; race factors "
                         + (", ".join(f"{r} x{float(f):g}" for r, f in sorted(rule.race_factors.items())) or "none"))
    if table.agility_points or table.intellect_points:
        lines.append(f"  External reference ({table.references.get('source', '?')}, build {table.references.get('build', '?')}) "
                     f"agrees with the panel for {len(table.agility_points)} agility and {len(table.intellect_points)} intellect rates and is used as the point estimate.")
    if table.client is not None:
        c = table.client
        lines.append(f"  Client per-level table: {len(c.rates)} classes, levels {min(min(v) for v in c.rates.values())}..{max(max(v) for v in c.rates.values())}; "
                     f"crit, dodge, spell crit, attack power, armor and mana work at every level (class constants fitted at levels {c.anchors[0]} and {c.anchors[1]}).")
        lines.append("  Level-60 agility per 1% crit from the client: " + ", ".join(
            f"{cls[:4]} {float(1 / r[60].agility_pct):.1f}" for cls, r in sorted(c.rates.items()) if 60 in r))
        no_ap = sorted(set(c.rates) - set(c.ap_constants))
        if no_ap:
            lines.append("  No attack power constant for: " + ", ".join(no_ap))
    for line in table.references.get("attack_power_overrides", []):
        lines.append(f"  Attack power rate from the reference, not the panel: {line}")
    if table.racial_dodge:
        lines.append("  Flat racial dodge already in the panel: " + ", ".join(f"{r} {float(v):+g}%" for r, v in sorted(table.racial_dodge.items())))
    return lines


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description="Fit or show the stat-to-figure rates.")
    ap.add_argument("command", choices=["fit", "show"])
    ap.add_argument("--data-dir", type=Path, default=repo_root / "data")
    ap.add_argument("--expected", type=Path, help="the client's PlayerExpectedStat.csv (default: data/external/PlayerExpectedStat.csv if present)")
    args = ap.parse_args(argv)
    external = args.data_dir / "external"
    out_file = external / "stat_rates.json"

    if args.command == "show":
        try:
            table = load_rates(out_file)
        except RatesError as exc:
            print(f"ERROR: {exc}")
            return 2
    else:
        try:
            stats = load_base_stats(external / "base_stats.tsv")
            panel = load_sheet_base(external / "sheet_base.tsv")
        except Exception as exc:  # noqa: BLE001 - say what is missing
            print(f"ERROR: {exc}")
            return 2
        if stats.problems:
            print("The base stats file has problems; run: python -m wowcalc.basestats")
            return 2
        expected = None
        expected_file = args.expected or external / "PlayerExpectedStat.csv"
        if expected_file.is_file():
            try:
                expected = load_expected(expected_file)
            except ValueError as exc:
                print(f"ERROR: {exc}")
                return 2
        elif args.expected:
            print(f"ERROR: no such file: {expected_file}")
            return 2
        table = fit_rates(stats, panel, expected=expected)
        refs_file = external / "rate_references.json"
        if refs_file.is_file():
            try:
                apply_references(table, json.loads(refs_file.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError) as exc:
                print(f"ERROR: cannot use {refs_file.name}: {exc}")
                return 2
        out_file.write_text(json.dumps(table.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {out_file}")
    print("\n".join(describe(table)))
    if table.problems:
        print(f"\n{len(table.problems)} problem(s):")
        for p in table.problems:
            print(f"  - {p}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
