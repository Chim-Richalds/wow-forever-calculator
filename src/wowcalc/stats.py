"""The character stat table: base stats plus what the chosen talents change.

    total = floor( (base + manual adjustment) x product of (1 + percent/100) )

The rounding rule comes from two in-game observations (October 2026):
  Mental Strength, base Intellect 120, ranks 1..5 -> 122, 124, 127, 129, 132
  Heart of the Wild, base Intellect 100, ranks 1..5 -> 102, 104, 106, 108, 110
120 x 1.04 = 124.8 shows 124 and 120 x 1.08 = 129.6 shows 129: the game rounds DOWN.
Arithmetic is exact (decimal). Plain floating point would be wrong here: 100 x 15% is 115,
but 100 * 1.15 evaluates to 114.99999999999999 and rounding down would show 114.

Three further kinds of talent effect are modelled (see effects.py), each checked in game:
  * Additive figures (crit, hit, dodge, parry): the talent's percentage points are simply
    summed. The data has no base value for these, so the sheet gives the talent
    contribution only, which is what the in-game before/after readings measure.
  * Attack Power from a stat (Mental Dexterity): floor(total stat x percent / 100), where
    the stat is the total AFTER the talents above. Observed: Intellect 87 -> +28, +58, +87
    at 33%, 67%, 100% (note the data says 67%; the rank-2 tooltip text you copied said 66%).
    The rounding is down: 87 x 33% = 28.71 showed 28.

Stat changes feed the panel figures too (rates.py, fitted from the game's own panel at
level 60): extra Agility raises melee crit, spell crit and dodge, extra Intellect raises
spell crit, and Strength and Agility raise Attack Power. So a talent such as Heart of
the Wild moves spell crit through the Intellect it adds. The rates are known to a range,
so those contributions carry an uncertainty, shown separately.

ASSUMPTIONS, not verified:
  * Several percentage bonuses to the same stat multiply (they do not add).
  * The base stats already include racial percentages (see basestats.py).
  * Percentages from several Attack-Power-from-stat talents on one stat are added before
    the rounding, and the stat used is the total with stat-percent talents applied.
  * Additive figures from different talents add (more than one is rare; none verified).
  * A "manual adjustment" is whatever the user's gear and buffs add to the stat; it
    is added before percentages, as the tooltip "total Intellect" implies.

Only effects the decoder understands (effects.STAT_PERCENT) change the table. Every
other part of a talent is listed in `not_modelled`, so the table never silently
pretends to be complete.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Iterable, Mapping

from .basestats import BaseStatTable
from .derived import PoolRule
from .perlevel import mana_total
from .rates import RateTable, StatEffects
from .sheetbase import SheetBase, SheetBaseError
from .effects import AP_FROM_STAT, SHEET_ADD, SHEET_FIGURES, SP_FROM_STAT, STAT_PERCENT, EffectBook, TalentEffect
from .trees import Node

# Where each sheet figure's base lives in the sheet-figures file: (section, label).
FIGURE_SOURCE = {
    "melee_crit": ("melee", "Crit Chance"),
    "spell_crit": ("spell", "Spell Crit"),
    "melee_hit": ("melee", "Hit Chance"),
    "spell_hit": ("spell", "Spell Hit"),
    "dodge": ("defense", "Dodge Chance"),
    "parry": ("defense", "Parry Chance"),
}
ATTACK_POWER_SOURCE = ("melee", "Attack Power")
ARMOR_SOURCE = ("defense", "Armor")
SPELL_POWER_SOURCE = ("spell", "Spell Damage")
HEALING_SOURCE = ("spell", "Healing")
# "block" has no panel source: the exported characters wore no shield. Its base is an assumption (derived.AssumedRules).
ADJUSTABLE = ("health", "mana", "strength", "agility", "stamina", "intellect", "spirit", "armor", "spell_power", "healing")

SHEET_STATS = ("strength", "agility", "stamina", "intellect", "spirit")


def apply_percents(base: int, percents: Iterable[float | str | Decimal]) -> int:
    """floor(base x product of (1 + p/100)), computed exactly."""
    factor = Decimal(1)
    for p in percents:
        factor *= (Decimal(100) + Decimal(str(p))) / Decimal(100)
    return math.floor(Decimal(base) * factor)


@dataclass(frozen=True)
class RankedTalent:
    node_id: str
    spell_id: str
    label: str
    ranks: int
    effects: tuple[TalentEffect, ...]


@dataclass(frozen=True)
class StatLine:
    stat: str
    base: int | None
    base_exact: bool  # False when the base is a straight-line estimate between supplied levels
    adjustment: int
    contributions: tuple[tuple[str, float], ...]  # (talent label, percent)
    total: int | None

    @property
    def talent_change(self) -> int | None:
        """What the talents add: the total with them minus the total without."""
        if self.total is None or self.base is None:
            return None
        return self.total - (self.base + self.adjustment)


@dataclass(frozen=True)
class FigureLine:
    """A sheet figure raised by talents (crit, hit, dodge, parry): points added, no base."""
    figure: str
    contributions: tuple[tuple[str, Decimal], ...]  # (talent label, percentage points)
    base: Decimal | None = None  # the panel figure without talents; None if the panel did not show it
    from_talent_stats: Decimal = Decimal(0)  # what the talents' extra Agility / Intellect add (fitted rates)
    from_adjustments: Decimal = Decimal(0)   # what the manual stat adjustments add (fitted rates)
    uncertainty: Decimal = Decimal(0)        # the most the two fitted parts could be off by

    @property
    def total(self) -> Decimal | None:
        return None if self.base is None else self.base + self.talent_bonus + self.from_talent_stats + self.from_adjustments

    @property
    def talent_total(self) -> Decimal:
        """Everything the talents add: their own points plus what their extra stats add."""
        return self.talent_bonus + self.from_talent_stats

    @property
    def talent_bonus(self) -> Decimal:
        return sum((p for _, p in self.contributions), Decimal(0))


@dataclass(frozen=True)
class AttackPowerLine:
    """Attack Power given by talents as a share of a stat."""
    contributions: tuple[tuple[str, str, Decimal], ...]  # (talent label, source stat, percent)
    bonus: int  # total Attack Power added
    base: int | None = None  # the panel's Attack Power without talents
    from_talent_stats: int = 0  # Strength / Agility the talents add, at the class's whole-number rule
    from_adjustments: int = 0

    @property
    def total(self) -> int | None:
        return None if self.base is None else self.base + self.bonus + self.from_talent_stats + self.from_adjustments

    @property
    def talent_total(self) -> int:
        return self.bonus + self.from_talent_stats



@dataclass(frozen=True)
class ArmorLine:
    """Armor and the damage reduction it gives against an attacker of your own level."""
    base: int | None          # the panel's armor without talents or adjustments
    from_talent_stats: int    # extra Agility from talents x armor per Agility
    from_adjustments: int     # extra Agility from adjustments x armor per Agility
    item_armor: int           # armor you added by hand (--adjust armor=N), e.g. the armor on your gear
    damage_reduction: Decimal | None  # percent, from the total

    @property
    def total(self) -> int | None:
        if self.base is None:
            return None
        return self.base + self.from_talent_stats + self.from_adjustments + self.item_armor


@dataclass(frozen=True)
class SpellPowerLine:
    """Spell damage or healing. The panel's figure has no items; gear and buffs are entered by hand. No talent is modelled to change it."""
    base: int | None
    item: int = 0   # amount you added by hand (--adjust spell_power=N or healing=N)
    contributions: tuple[tuple[str, str, Decimal], ...] = ()   # (talent label, source stat, percent of it)
    from_talents: int = 0   # floor(stat total x summed percent / 100) per source stat, summed; rounding NOT checked in game

    @property
    def total(self) -> int | None:
        return None if self.base is None else self.base + self.from_talents + self.item


@dataclass(frozen=True)
class NotModelled:
    label: str
    ranks: int
    parts: tuple[tuple[int, str, str], ...]  # (effect index, kind, why)


@dataclass
class Sheet:
    class_name: str
    race_name: str
    level: int
    lines: dict[str, StatLine]
    figures: dict[str, FigureLine] = field(default_factory=dict)
    attack_power: AttackPowerLine = field(default_factory=lambda: AttackPowerLine((), 0))
    armor: ArmorLine = field(default_factory=lambda: ArmorLine(None, 0, 0, 0, None))
    spell_power: SpellPowerLine = field(default_factory=lambda: SpellPowerLine(None))
    healing: SpellPowerLine = field(default_factory=lambda: SpellPowerLine(None))
    block_value_from_strength: int | None = None  # ASSUMED rule; only with a shield
    not_modelled: list[NotModelled] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def build_sheet(
    table: BaseStatTable,
    class_name: str,
    race_name: str,
    level: int,
    talents: Iterable[RankedTalent],
    adjustments: Mapping[str, int] | None = None,
    sheet_base: SheetBase | None = None,
    rates: RateTable | None = None,
    shield: bool = False,
) -> Sheet:
    base = table.at(class_name, race_name, level)  # KeyError / ValueError for a bad request
    adjustments = dict(adjustments or {})
    unknown = sorted(set(adjustments) - set(ADJUSTABLE))
    if unknown:
        raise ValueError(f"cannot adjust {', '.join(map(repr, unknown))}; choose from {', '.join(ADJUSTABLE)}")
    contributions: dict[str, list[tuple[str, float]]] = {s: [] for s in SHEET_STATS}
    not_modelled: list[NotModelled] = []
    figure_parts: dict[str, list[tuple[str, Decimal]]] = {f: [] for f in SHEET_FIGURES}
    ap_parts: list[tuple[str, str, Decimal]] = []
    sp_parts: dict[str, list[tuple[str, str, Decimal]]] = {"spell_power": [], "healing": []}

    for talent in talents:
        if talent.ranks <= 0:
            continue
        parts: list[tuple[int, str, str]] = []
        for effect in talent.effects:
            if effect.modelled and talent.ranks in effect.by_rank:
                value = effect.by_rank[talent.ranks]
                if effect.kind == STAT_PERCENT:
                    for stat in effect.stats:
                        contributions[stat].append((talent.label, value))
                elif effect.kind == SHEET_ADD:
                    for figure in effect.figures:
                        figure_parts[figure].append((talent.label, Decimal(str(value))))
                elif effect.kind == AP_FROM_STAT:
                    ap_parts.append((talent.label, effect.stats[0], Decimal(str(value))))
                elif effect.kind == SP_FROM_STAT:
                    sp_parts[effect.figures[0]].append((talent.label, effect.stats[0], Decimal(str(value))))
            else:
                why = effect.note or effect.kind
                if effect.modelled:
                    why = f"no value recorded for rank {talent.ranks}"
                parts.append((effect.effect_index, effect.kind, why))
        if parts or not talent.effects:
            not_modelled.append(NotModelled(talent.label, talent.ranks, tuple(parts) or ((-1, "none", "the talent has no effects in the data"),)))

    lines: dict[str, StatLine] = {}
    for stat in ("health", "mana") + SHEET_STATS:
        b = base[stat]
        adj = adjustments.get(stat, 0)
        if b.value is None:
            lines[stat] = StatLine(stat, None, b.exact, adj, (), None)
            continue
        contribs = tuple(contributions.get(stat, ()))
        total = apply_percents(b.value + adj, [p for _, p in contribs])
        lines[stat] = StatLine(stat, b.value, b.exact, adj, contribs, total)
    panel: dict = {}
    notes: list[str] = []
    exported_panel = False   # the panel for exactly this level was exported (not estimated)
    if sheet_base is not None:
        try:
            panel = sheet_base.at(class_name, race_name, level)
            exported_panel = True
        except (KeyError, SheetBaseError) as exc:
            estimate = None
            if rates is not None and isinstance(exc, SheetBaseError):
                reference = None
                for anchor in (rates.level, 1):
                    try:
                        reference = {k: v for k, v in sheet_base.at(class_name, race_name, anchor).items()}
                        break
                    except (KeyError, SheetBaseError):
                        continue
                needed = [lines["strength"], lines["agility"], lines["intellect"]]
                if all(l.base is not None for l in needed):
                    estimate = rates.panel_estimate(class_name, race_name, level, lines["strength"].base, lines["agility"].base,
                                                    lines["intellect"].base, reference)
            if estimate:
                panel = estimate
                exact = all(l.base_exact for l in (lines["strength"], lines["agility"], lines["intellect"]))
                notes.append(f"there is no character panel for level {level}; crit, dodge, attack power and armor are computed from the "
                             f"client's per-level rates and class constants fitted at levels {rates.client.anchors[0]} and {rates.client.anchors[1]} "
                             f"(an inference between them, each within about 0.01){'' if exact else ' and from ESTIMATED base stats'}")
            else:
                notes.append(f"no base crit/hit/dodge/parry/attack power: {exc.args[0] if exc.args else exc}")

    def panel_value(source: tuple[str, str]) -> Decimal | None:
        found = panel.get(source)
        return None if found is None else found.value

    from_talents = from_adjust = err = None
    if rates is not None:
        from_talents, from_adjust, err = _stat_effects(rates, class_name, level, lines, notes)

    def derived(figure: str, part: StatEffects | None) -> Decimal:
        if part is None:
            return Decimal(0)
        value = {"melee_crit": part.melee_crit, "spell_crit": part.spell_crit, "dodge": part.dodge}.get(figure)
        return Decimal(0) if value is None else Decimal(value.numerator) / Decimal(value.denominator)

    block_base = None
    if shield:
        if rates is not None and class_name in rates.assumed.shield_classes:
            block_base = rates.assumed.block_base_chance
            notes.append(f"block: the {float(block_base):g}% base chance with a shield is confirmed from the game's own text, not from the panel (no items)")
        elif rates is None:
            notes.append("block: no rates file, so no base block chance is known")
        else:
            notes.append(f"block: {class_name} is not one of {', '.join(rates.assumed.shield_classes)}; no base block chance")

    def base_of(figure: str):
        if figure == "block":
            return None if block_base is None else Decimal(block_base.numerator) / Decimal(block_base.denominator)
        return panel_value(FIGURE_SOURCE[figure]) if figure in FIGURE_SOURCE else None

    gains_parry = exported_panel and panel_value(FIGURE_SOURCE["parry"]) is None and bool(figure_parts["parry"])
    if gains_parry:
        notes.append(f"parry: the panel shows no parry for {class_name}, so a talent that adds parry is counted from 0 "
                     "(assuming the class cannot parry without it; not checked in game)")

    if figure_parts["casting_regen"]:
        notes.append("mana regeneration while casting: the base is taken as 0% (the talent texts say what they 'allow'), "
                     "the talents add; how much mana that is depends on Spirit and is not computed")

    def base_with_parry(figure: str):
        base = base_of(figure)
        if figure == "parry" and base is None and gains_parry:
            return Decimal(0)
        return Decimal(0) if figure == "casting_regen" and figure_parts["casting_regen"] else base

    figures = {f: FigureLine(f, tuple(parts), base_with_parry(f),
                             derived(f, from_talents), derived(f, from_adjust), abs(derived(f, err)))
               for f, parts in figure_parts.items()}
    armor_line = _armor(rates, level, lines, adjustments, panel_value(ARMOR_SOURCE), from_talents is not None,
                        rates is not None and rates.covers(class_name, level))
    _apply_pools(rates, class_name, race_name, level, lines, adjustments, notes)
    block_value = None
    if shield and rates is not None and class_name in rates.assumed.shield_classes and lines["strength"].total is not None:
        block_value = lines["strength"].total // rates.assumed.strength_per_block_value
        notes.append(f"block value from Strength (1 per {rates.assumed.strength_per_block_value}) is confirmed from the game's own text; Strength does not raise block chance")
    attack = _attack_power(ap_parts, lines)
    ap_base = panel_value(ATTACK_POWER_SOURCE)
    attack = AttackPowerLine(attack.contributions, attack.bonus, None if ap_base is None else int(ap_base),
                             0 if from_talents is None else from_talents.attack_power,
                             0 if from_adjust is None else from_adjust.attack_power)
    sp_base = panel_value(SPELL_POWER_SOURCE)
    spell_power = _spell_power(sp_parts["spell_power"], lines, sp_base, int(adjustments.get("spell_power", 0)))
    heal_base = panel_value(HEALING_SOURCE)
    healing = _spell_power(sp_parts["healing"], lines, heal_base, int(adjustments.get("healing", 0)))
    if sp_parts["spell_power"] or sp_parts["healing"]:
        notes.append("spell power and healing from a stat: the amount is rounded down; the game's rounding has not been checked")
    return Sheet(class_name, race_name, level, lines, figures, attack, armor_line, spell_power, healing, block_value, not_modelled, notes)


def _armor(rates: RateTable | None, level: int, lines: dict[str, StatLine], adjustments, panel_armor, rates_applied: bool,
           covered: bool = False) -> ArmorLine:
    item = int(adjustments.get("armor", 0))
    if rates is None or rates.armor is None or panel_armor is None or not covered or not rates_applied:
        total = None if panel_armor is None else int(panel_armor) + item
        dr = None
        if total is not None and rates is not None and rates.armor is not None:
            dr = _as_decimal(rates.armor.damage_reduction(total, level))
        return ArmorLine(None if panel_armor is None else int(panel_armor), 0, 0, item, dr)
    k = rates.armor.per_agility
    agi = lines["agility"]
    from_adjust = k * agi.adjustment
    from_talents = k * (agi.total - agi.base - agi.adjustment) if agi.total is not None and agi.base is not None else 0
    total = int(panel_armor) + from_talents + from_adjust + item
    return ArmorLine(int(panel_armor), from_talents, from_adjust, item, _as_decimal(rates.armor.damage_reduction(total, level)))


def _as_decimal(value) -> Decimal:
    return Decimal(value.numerator) / Decimal(value.denominator)


def _apply_pools(rates: RateTable | None, class_name: str, race: str, level: int, lines: dict[str, StatLine], adjustments, notes: list[str]) -> None:
    """Health from Stamina and mana from Intellect, replacing the plain base + adjustment when the rule
    reproduces the base value exactly."""
    if rates is None:
        return
    if level != rates.level:
        _apply_client_mana(rates, class_name, race, level, lines, notes)
        return
    for pool, stat, rule in (("health", "stamina", rates.health), ("mana", "intellect", rates.mana)):
        line, source = lines[pool], lines[stat]
        if rule is None or class_name not in rule.constants or line.base is None or source.total is None or source.base is None:
            continue
        if rule.total(class_name, race, source.base) != line.base:
            notes.append(f"the {pool} rule does not reproduce the base {pool} of a {race} {class_name}; extra {stat} does not change it")
            continue
        total = rule.total(class_name, race, source.total) + line.adjustment
        lines[pool] = StatLine(pool, line.base, line.base_exact, line.adjustment, line.contributions, total)


def _apply_client_mana(rates: RateTable, class_name: str, race: str, level: int, lines: dict[str, StatLine], notes: list[str]) -> None:
    """Mana at any level from the client's BaseMana and the Intellect rule (perlevel.mana_total)."""
    rate = None if rates.client is None else rates.client.rate(class_name, level)
    line, source = lines["mana"], lines["intellect"]
    if rate is None or not rate.base_mana or source.base is None or source.total is None:
        return
    factor = Fraction(1) if rates.mana is None else rates.mana.race_factors.get(race, Fraction(1))
    formula = mana_total(rate.base_mana, source.base, factor)
    if line.base is not None and line.base_exact:
        if formula != line.base:
            notes.append(f"the client's mana rule gives {formula} for the base mana of a {race} {class_name} at level {level}, "
                         f"but the base stats say {line.base}; extra Intellect does not change mana")
            return
        base, exact = line.base, True
    else:
        base, exact = formula, source.base_exact
        if not exact:
            notes.append(f"base mana at level {level} is computed from the client's table and an ESTIMATED base Intellect")
    total = mana_total(rate.base_mana, source.total, factor) + line.adjustment
    lines["mana"] = StatLine("mana", base, exact, line.adjustment, line.contributions, total)


def _stat_effects(rates: RateTable, class_name: str, level: int, lines: dict[str, StatLine], notes: list[str]):
    """(effect of the talents' stat changes, effect of the adjustments, uncertainty), or Nones."""
    if not rates.covers(class_name, level):
        notes.append(f"stat-to-crit/dodge/attack-power rates were fitted at level {rates.level}; not applied at level {level}"
                     " (add the client's PlayerExpectedStat table to get every level)")
        return None, None, None
    if class_name not in rates.agility or class_name not in rates.attack_power:
        notes.append(f"no fitted rates for {class_name}; extra stats do not change crit, dodge or attack power")
        return None, None, None
    needed = [lines[s] for s in ("strength", "agility", "intellect")]
    if any(l.total is None or l.base is None for l in needed):
        return None, None, None
    d_talents = {l.stat: l.total - l.base - l.adjustment for l in needed}
    d_adjust = {l.stat: l.adjustment for l in needed}
    best_t, err_t = rates.effects(class_name, d_talents["strength"], d_talents["agility"], d_talents["intellect"], level)
    best_a, err_a = rates.effects(class_name, d_adjust["strength"], d_adjust["agility"], d_adjust["intellect"], level)
    return best_t, best_a, err_t + err_a


def _attack_power(parts: list[tuple[str, str, Decimal]], lines: dict[str, StatLine]) -> AttackPowerLine:
    """floor(stat total x summed percent / 100), per source stat, then summed."""
    bonus = 0
    for stat in {p[1] for p in parts}:
        total = lines[stat].total
        if total is None:
            continue
        percent = sum((p[2] for p in parts if p[1] == stat), Decimal(0))
        bonus += math.floor(Decimal(total) * percent / Decimal(100))
    return AttackPowerLine(tuple(parts), bonus)


def _spell_power(parts: list[tuple[str, str, Decimal]], lines: dict[str, StatLine], panel_base, item: int) -> SpellPowerLine:
    """Spell damage or healing: the panel figure, plus floor(stat total x summed percent / 100) per source stat."""
    bonus = 0
    for stat in {p[1] for p in parts}:
        total = lines[stat].total
        if total is None:
            continue
        percent = sum((p[2] for p in parts if p[1] == stat), Decimal(0))
        bonus += math.floor(Decimal(total) * percent / Decimal(100))
    return SpellPowerLine(None if panel_base is None else int(panel_base), item, tuple(parts), bonus)


def ranked(book: EffectBook, node: Node, label: str, ranks: int) -> RankedTalent:
    """A talent at `ranks`, with its effects decoded from `book`."""
    return RankedTalent(node.id, node.spell_id, label, ranks, tuple(book.decode(node)))
