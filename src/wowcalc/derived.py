"""Rules that turn primary stats into armor, health, mana and block, and how each was
established. Everything here is checked against the game's own character panel
(`sheet_base.tsv`) except the block rules, which the panel cannot show (the exported
characters wore no items) and which are therefore labelled ASSUMED.

  armor           2 per Agility. Checked on every class/race at levels 1 and 60: 111 of
                  112 rows agree; the one that does not (Warrior / Night Elf, level 1) is a
                  base-stat typo (see rates.py notes), not a different rule.
  damage reduction  armor / (armor + 85 x level + 400), the Classic formula. Checked on all
                  112 rows; worst difference 0.00487 against a display step of 0.005.
  health          floor((class constant + 10 x Stamina) x race factor), level 60.
  mana            floor((class constant + 15 x Intellect) x race factor), level 60.
                  The race factor is 1 except Undead health (x1.05) and Gnome mana (x1.05);
                  both were found from the data, not assumed, and reproduce every row exactly
                  (with rounding down).
  block chance   5% base while wearing a shield (the user confirmed it from the game's own
                  text; the no-gear panel cannot show it). Against an attacker of a different
                  level it changes by 0.5% per level for a mob and 0.2% per level for a player
                  (same source). The direction (a higher-level attacker lowers it) is inferred,
                  not quoted.
  block value     Strength gives block VALUE, 1 per 20, for Warrior, Paladin and Shaman
                  (confirmed by the user from the game's text; two public sources agree).
                  Strength does not give block CHANCE in any source found.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction

DAMAGE_REDUCTION_PER_LEVEL = 85
DAMAGE_REDUCTION_CONSTANT = 400
DISPLAY_STEP = Fraction(5, 1000)  # the panel shows two decimals


@dataclass(frozen=True)
class ArmorRule:
    per_agility: int
    worst_dr_error: Fraction  # largest gap between the formula and the panel's Damage Reduction

    def damage_reduction(self, armor: int, level: int) -> Fraction:
        """Percent. Armor / (armor + 85 x level + 400)."""
        if armor <= 0:
            return Fraction(0)
        return 100 * Fraction(armor, armor + DAMAGE_REDUCTION_PER_LEVEL * level + DAMAGE_REDUCTION_CONSTANT)


@dataclass(frozen=True)
class PoolRule:
    """Health or mana: floor((class constant + per_point x stat + adjustment) x race factor)."""
    per_point: int
    constants: dict[str, int] = field(default_factory=dict)
    race_factors: dict[str, Fraction] = field(default_factory=dict)

    def total(self, class_name: str, race: str, stat: int, adjustment: int = 0) -> int:
        constant = self.constants[class_name]  # KeyError: this class has no such pool
        return math.floor((constant + self.per_point * stat + adjustment) * self.race_factors.get(race, Fraction(1)))


@dataclass(frozen=True)
class AssumedRules:
    """Rules used but not verified. Shown to the user whenever they take effect."""
    block_base_chance: Fraction = Fraction(5)
    shield_classes: tuple[str, ...] = ("Paladin", "Shaman", "Warrior")
    strength_per_block_value: int = 20
    block_per_level_mob: Fraction = Fraction(1, 2)      # percentage points per level of difference
    block_per_level_player: Fraction = Fraction(1, 5)

    def block_against(self, attacker_level: int, defender_level: int, attacker_is_player: bool) -> Fraction:
        """Block chance against an attacker of `attacker_level` (percent). A higher-level attacker lowers it,
        a lower-level one raises it (direction inferred, magnitudes from the game's text). Never below 0."""
        per_level = self.block_per_level_player if attacker_is_player else self.block_per_level_mob
        return max(Fraction(0), self.block_base_chance - per_level * (attacker_level - defender_level))

    def to_json(self) -> dict:
        return {"block_base_chance": str(self.block_base_chance), "shield_classes": list(self.shield_classes),
                "strength_per_block_value": self.strength_per_block_value,
                "block_per_level_mob": str(self.block_per_level_mob),
                "block_per_level_player": str(self.block_per_level_player),
                "status": "block chance: confirmed by the user from the game's text (not checkable from a panel exported without items); "
                          "block value from Strength: confirmed by the user from the game's text"}

    @classmethod
    def from_json(cls, data: dict) -> "AssumedRules":
        return cls(Fraction(data["block_base_chance"]), tuple(data["shield_classes"]), int(data["strength_per_block_value"]),
                   Fraction(data.get("block_per_level_mob", "1/2")), Fraction(data.get("block_per_level_player", "1/5")))


# --------------------------------------------------------------------------
# Fitting
# --------------------------------------------------------------------------

def fit_armor(rows: list[dict], dr_rows: list[tuple[int, Fraction, Fraction]], problems: list[str]) -> ArmorRule | None:
    """`rows`: level-60 rows with 'agility' and 'armor'. `dr_rows`: (level, armor, shown damage reduction)
    for every level supplied. Problems are appended, never hidden."""
    usable = [r for r in rows if r.get("armor") is not None]
    if not usable:
        problems.append("no armor figures to fit")
        return None
    ratios = Counter()
    for r in usable:
        if r["agility"] and Fraction(r["armor"]) % r["agility"] == 0:
            ratios[int(Fraction(r["armor"]) / r["agility"])] += 1
    if not ratios:
        problems.append("armor is not a whole-number multiple of Agility")
        return None
    k = ratios.most_common(1)[0][0]
    for r in usable:
        if Fraction(r["armor"]) != k * r["agility"]:
            problems.append(f"{r['race']} {r['class']}: armor {r['armor']} but {k} x Agility is {k * r['agility']}")
    worst = Fraction(0)
    for level, armor, shown in dr_rows:
        predicted = ArmorRule(k, Fraction(0)).damage_reduction(int(armor), level)
        worst = max(worst, abs(predicted - shown))
    if worst > DISPLAY_STEP:
        problems.append(f"damage reduction differs from the Classic formula by up to {float(worst):.4f}")
    return ArmorRule(k, worst)


def fit_pool(rows: list[dict], stat: str, key: str, problems: list[str], label: str) -> PoolRule | None:
    """Fit health (key 'health', stat 'stamina') or mana (key 'mana', stat 'intellect')."""
    usable = [r for r in rows if r.get(key) is not None]
    if not usable:
        return None
    classes = sorted({r["class"] for r in usable})
    solutions = []
    for k in range(1, 31):
        constants: dict[str, int] = {}
        ok = True
        for cls in classes:
            counts = Counter(int(r[key]) - k * int(r[stat]) for r in usable if r["class"] == cls)
            ranked = counts.most_common(2)
            if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
                ok = False  # a tie: cannot tell which races are the ordinary ones
                break
            constants[cls] = ranked[0][0]
        if not ok:
            continue
        deviating = [r for r in usable if int(r[key]) != constants[r["class"]] + k * int(r[stat])]
        factors: dict[str, Fraction] = {}
        for race in sorted({r["race"] for r in deviating}):
            mine = [r for r in deviating if r["race"] == race]
            fits = [Fraction(100 + j, 100) for j in range(-50, 51) if j != 0 and
                    all(math.floor((constants[r["class"]] + k * int(r[stat])) * Fraction(100 + j, 100)) == int(r[key]) for r in mine)]
            if len(fits) != 1:
                ok = False
                break
            factors[race] = fits[0]
        if not ok:
            continue
        # the ordinary races of a deviating race's classes must be unaffected: they matched with factor 1
        solutions.append((len(deviating), k, constants, factors))
    if not solutions:
        problems.append(f"{label}: no whole-number rule per {stat} (with at most one flat race factor) reproduces every row")
        return None
    solutions.sort(key=lambda s: s[0])  # fewest rows needing a race factor first
    if len(solutions) > 1 and solutions[0][0] == solutions[1][0] and solutions[0][1] != solutions[1][1]:
        problems.append(f"{label}: more than one rule fits equally well (per {stat}: {solutions[0][1]} or {solutions[1][1]})")
        return None
    _, k, constants, factors = solutions[0]
    return PoolRule(k, constants, factors)
