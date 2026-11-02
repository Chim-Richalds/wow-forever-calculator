"""The combat model, phase 1 (docs/combat-model-plan.md): damage and healing per cast for each spell.

For the highest rank of a spell the level allows, from its own data:

  direct      SpellEffect Effect 2 (damage) or 10 (healing): the base amount, grown per level above the
              spell's base level (capped at its maximum level), spread by `Variance`, plus
              EffectBonusCoefficient x spell power (healing power for heals).
  over time   Effect 6 (or 35, party aura) with aura 3 (damage), 8 (healing) or 53 (leech, as damage): the same per tick,
              times the ticks in the spell's duration (one tick per EffectAuraPeriod).
  triggered   aura 23 (a periodic trigger, e.g. Arcane Missiles): the triggered spell's direct damage
              per tick, followed one step.

Anything else (ground effects such as Blizzard, scripted damage) is reported as not calculated yet.
Weapon attacks are phase 2. The rules the data does not hold are ASSUMED (the original game's
published rules) and labelled wherever they are used; see ASSUMPTIONS.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .dataset import Dataset
from .dataset import as_int as _int
from .spells import BONUS_OPS, ClassSpell, SpellBook, SpellModifier, _rows, applies

EFFECT_DAMAGE, EFFECT_HEAL = 2, 10
AURA_EFFECTS = frozenset({6, 35})   # apply aura; apply aura to the party (Tranquility)
TARGET_CASTER = 1                   # damage aimed at the caster (Hellfire's own burn) is not counted
CHANNELED = 0x4 | 0x40              # SpellMisc Attributes_1 bits set on channelled spells (Arcane Missiles, Mind Flay, Hellfire, Tranquility)
AURA_PERIODIC_DAMAGE, AURA_PERIODIC_HEAL, AURA_PERIODIC_LEECH, AURA_PERIODIC_TRIGGER = 3, 8, 53, 23

# Rules the game's tables do not hold. The original game's published values, used until a reading
# checks them (decision 2026-11-02); every result says which it relies on.
SPELL_CRIT_MULTIPLIER = 1.5          # a spell crit does 150%; talents' "critical strike bonus" raises the extra 50%
SPELL_MISS_BY_LEVEL = {0: 4.0, 1: 5.0, 2: 6.0, 3: 17.0}   # % against a target this many levels above you
SPELL_MISS_MINIMUM = 1.0
ASSUMPTIONS = (
    "spell crits do 150% (talent crit bonuses raise the extra 50%); damage and healing over time do not crit",
    "spell miss chance 4% against your level, 5% / 6% / 17% at +1 / +2 / +3 levels, less spell hit, at least 1%; heals always land",
    "an effect's amount grows by its per-level value from the spell's base level to your level, capped at its maximum level, "
    "and rolls within plus or minus half its Variance",
    "talent 'damage or healing' bonuses apply to direct amounts, 'periodic' bonuses to ticks",
    "the hits a channel triggers (Arcane Missiles' missiles) can crit, and take the channel's talent bonuses",
    "partial resists and the target's armor are not counted",
    "physical abilities without weapon damage (Bloodthirst, Hamstring, Rend, Eviscerate) and Hunter shots such as Arcane Shot "
    "count their base amount only (finishers at one combo point): any attack-power scaling is in the game's code, not its data; "
    "they use the melee or ranged hit table and 200% crits",
)


@dataclass(frozen=True)
class Target:
    label: str
    level_difference: int   # target level minus yours


SAME_LEVEL, BOSS = Target("a target of your level", 0), Target("a boss (your level + 3)", 3)
TARGETS = {"same": SAME_LEVEL, "boss": BOSS}


@dataclass(frozen=True)
class SpellNumbers:
    spell: str
    spell_id: str
    kind: str                     # "damage" or "healing"
    direct_min: float = 0.0
    direct_max: float = 0.0
    periodic_total: float = 0.0   # all ticks (including triggered hits)
    ticks: int = 0
    triggered_total: float = 0.0  # the part of periodic_total that is triggered hits, which can crit
    crit_chance: float = 0.0      # percent, direct part only
    crit_multiplier: float = SPELL_CRIT_MULTIPLIER
    hit_chance: float = 100.0     # percent
    expected: float = 0.0         # per cast, with crit and hit
    per_second: float | None = None   # expected / max(cast time, global cooldown)
    per_cost: float | None = None     # expected per point of the spell's cost
    cost_unit: str = ""
    power_part: float = 0.0       # how much of the average comes from spell or healing power


def _amount(row: dict, level: int, levels: dict) -> float:
    """An effect's base amount at the caster's level (ASSUMED per-level rule)."""
    base = float(row.get("EffectBasePointsF") or 0)
    per_level = float(row.get("EffectRealPointsPerLevel") or 0)
    if per_level:
        lv = levels.get(row["SpellID"], {})
        top = _int(lv.get("MaxLevel"))
        at = min(level, top) if top else level
        base += per_level * max(0, at - _int(lv.get("BaseLevel")))
    return base


class CombatBook:
    """Spell effects, levels and talent bonuses needed to put numbers on spells; read once."""

    def __init__(self, ds: Dataset, book: SpellBook):
        self.book = book
        self.effects: dict[str, list[dict]] = defaultdict(list)
        for r in _rows(ds, "SpellEffect", {"SpellID", "Effect"}):
            self.effects[r["SpellID"]].append(r)
        self.levels = {r["SpellID"]: r for r in _rows(ds, "SpellLevels", {"SpellID"})}

    def _parts(self, spell_id: str, level: int, power: float, depth: int = 0):
        """(kind, direct min, direct max, direct power part, per-tick amount, tick power part, ticks,
        triggered per-tick amount including power), or None."""
        rows = sorted(self.effects.get(spell_id, []), key=lambda r: _int(r["EffectIndex"]))
        kind, lo, hi, d_power, tick, t_power, ticks, trig = None, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0.0
        base = self.book.misc.get(spell_id, {})
        duration = self.book.durations.get(base.get("DurationIndex", ""), 0)
        for r in rows:
            effect, aura = _int(r.get("Effect")), _int(r.get("EffectAura"))
            coeff = float(r.get("EffectBonusCoefficient") or 0)
            amount = _amount(r, level, self.levels)
            self_damage = _int(r.get("ImplicitTarget_0")) == TARGET_CASTER and (
                effect == EFFECT_DAMAGE or aura in (AURA_PERIODIC_DAMAGE, AURA_PERIODIC_LEECH))
            if self_damage:
                continue
            if effect in (EFFECT_DAMAGE, EFFECT_HEAL):
                k = "damage" if effect == EFFECT_DAMAGE else "healing"
                if kind not in (None, k):
                    return None   # a spell that both damages and heals: not calculated yet
                kind = k
                spread = float(r.get("Variance") or 0) / 2
                lo, hi = lo + amount * (1 - spread), hi + amount * (1 + spread)
                d_power += coeff * power
            elif effect in AURA_EFFECTS and aura in (AURA_PERIODIC_DAMAGE, AURA_PERIODIC_HEAL, AURA_PERIODIC_LEECH):
                period = _int(r.get("EffectAuraPeriod"))
                if not period or not duration:
                    continue
                k = "healing" if aura == AURA_PERIODIC_HEAL else "damage"
                if kind not in (None, k):
                    return None
                kind = k
                n = duration // period
                ticks, tick, t_power = max(ticks, n), tick + amount, t_power + coeff * power
            elif effect in AURA_EFFECTS and aura == AURA_PERIODIC_TRIGGER and depth == 0:
                period, triggered = _int(r.get("EffectAuraPeriod")), r.get("EffectTriggerSpell", "")
                inner = self._parts(triggered, level, power, depth + 1) if triggered not in ("", "0") else None
                if not period or not duration or inner is None or inner[0] is None:
                    continue
                if kind not in (None, inner[0]):
                    return None
                kind = inner[0]
                n = duration // period
                ticks, trig = max(ticks, n), trig + (inner[1] + inner[2]) / 2 + inner[3]
        if kind is None:
            return None
        return kind, lo, hi, d_power, tick, t_power, ticks, trig

    def numbers(self, spell: ClassSpell, level: int, spell_power: float, healing_power: float, spell_crit: float,
                spell_hit: float, target: Target, bonuses: dict[str, float], cast_time: float | None,
                global_cooldown: float | None, cost: float | None, cost_unit: str,
                attack: tuple[float, float, bool] | None = None) -> SpellNumbers | None:
        """`bonuses`: this spell's talent bonuses by BONUS_OPS name (percent or percentage points).
        `attack`: (melee crit, melee hit, ranged) for an attack rather than a spell (a physical ability, or
        any ability of a Warrior, Rogue or Hunter): the melee or ranged hit table and 200% crits apply, and
        spell power does not."""
        base = self.book.base(spell, level)
        healing = self._parts(base.spell_id, level, 0, 0)
        if healing is None:
            return None
        power = 0.0 if attack else healing_power if healing[0] == "healing" else spell_power
        kind, lo, hi, d_power, tick, t_power, ticks, trig = self._parts(base.spell_id, level, power)
        direct = 1 + bonuses.get("damage or healing", 0) / 100
        periodic = 1 + bonuses.get("periodic damage or healing", 0) / 100
        lo, hi, d_power = (lo + d_power) * direct, (hi + d_power) * direct, d_power * direct
        total_ticks = (tick + t_power) * ticks * periodic
        triggered = trig * ticks * direct
        crit = max(0.0, min(100.0, spell_crit + bonuses.get("critical strike chance", 0))) if lo or hi or triggered else 0.0
        multiplier = 1 + (SPELL_CRIT_MULTIPLIER - 1) * (1 + bonuses.get("critical strike bonus", 0) / 100)
        if kind == "healing":
            hit = 100.0
        else:
            miss = SPELL_MISS_BY_LEVEL.get(max(0, min(3, target.level_difference)), SPELL_MISS_BY_LEVEL[3])
            hit = 100 - max(SPELL_MISS_MINIMUM, miss - spell_hit - bonuses.get("hit chance", 0))
        average_direct = (lo + hi) / 2
        if attack and kind == "damage":
            a_crit, a_hit, a_ranged = attack
            miss, dodge, _, _, crit = _hit_table(target, a_hit + bonuses.get("hit chance", 0),
                                                 (a_crit + bonuses.get("critical strike chance", 0)) if lo or hi else 0.0,
                                                 a_ranged, white=False)
            multiplier = 1 + (MELEE_CRIT_MULTIPLIER - 1) * (1 + bonuses.get("critical strike bonus", 0) / 100)
            hit = 100 - miss - dodge
            expected = (average_direct * ((hit - crit) + crit * multiplier) / 100 + triggered * hit / 100
                        + total_ticks * hit / 100)
        else:
            expected = ((average_direct + triggered) * (1 + crit / 100 * (multiplier - 1)) + total_ticks) * hit / 100
        channel = _int(self.book.misc.get(base.spell_id, {}).get("Attributes_1")) & CHANNELED
        duration = self.book.durations.get(self.book.misc.get(base.spell_id, {}).get("DurationIndex", ""), 0)
        # time the spell takes you: a channel's length, else the cast time or the global cooldown (at least 1 s)
        time = (duration if channel and duration else max(t for t in (cast_time or 0, global_cooldown or 0, 1000) if t)) / 1000
        return SpellNumbers(spell.name, base.spell_id, kind, lo, hi, total_ticks + triggered, ticks, triggered, crit, multiplier, hit,
                            expected, expected / time if time else None, expected / cost if cost else None, cost_unit,
                            d_power + t_power * ticks * periodic)


def spell_numbers(combat: CombatBook, spells: dict[str, ClassSpell], mods: list[SpellModifier], class_name: str, level: int,
                  ranks: dict[str, int], spell_power: float, healing_power: float, spell_crit: float, spell_hit: float,
                  target: Target, changes: dict[tuple[str, str], float], melee_crit: float = 0.0,
                  melee_hit: float = 0.0) -> tuple[list[SpellNumbers], list[str]]:
    """Numbers for every class spell the level allows that deals damage or heals. `changes` holds the
    talent-changed cast time, global cooldown and cost per (spell, property) from spells.spell_changes.
    Returns (numbers, names of spells whose damage or healing is not calculated yet)."""
    bonuses: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for m in mods:
        r = ranks.get(m.spell_id, 0)
        if m.class_name == class_name and r and m.op in BONUS_OPS and applies(m) and r in m.by_rank:
            for name in m.matched:
                bonuses[name][BONUS_OPS[m.op][0]] += m.by_rank[r]
    out, skipped = [], []
    for name, spell in sorted(spells.items()):
        base = combat.book.base(spell, level)
        if base.level > level:
            continue
        if not any(_int(r.get("Effect")) in (EFFECT_DAMAGE, EFFECT_HEAL) or
                   (_int(r.get("Effect")) in AURA_EFFECTS and _int(r.get("EffectAura")) in
                    (AURA_PERIODIC_DAMAGE, AURA_PERIODIC_HEAL, AURA_PERIODIC_LEECH, AURA_PERIODIC_TRIGGER))
                   for r in combat.effects.get(base.spell_id, [])):
            continue
        attack = None
        if class_name in ATTACK_CLASSES or set(spell.schools) == {"Physical"}:
            attack = (melee_crit, melee_hit, bool(base.range and base.range > RANGED_MIN_YARDS))
        n = combat.numbers(spell, level, spell_power, healing_power, spell_crit, spell_hit, target, bonuses.get(name, {}),
                           changes.get((name, "cast time"), base.cast_time), changes.get((name, "global cooldown"), base.global_cooldown),
                           changes.get((name, "cost"), base.cost) if base.cost_unit and not base.cost_unit.startswith("%") else None,
                           base.cost_unit, attack)
        if n is None or (n.direct_max == 0 and n.periodic_total == 0):
            skipped.append(name)
        else:
            out.append(n)
    return out, skipped


# ---- phase 2: weapon attacks (docs/combat-model-plan.md)

EFFECT_WEAPON_NOSCHOOL, EFFECT_WEAPON, EFFECT_WEAPON_NORMALIZED, EFFECT_WEAPON_PERCENT = 17, 58, 121, 31
WEAPON_EFFECTS = frozenset({EFFECT_WEAPON_NOSCHOOL, EFFECT_WEAPON, EFFECT_WEAPON_NORMALIZED, EFFECT_WEAPON_PERCENT})
NORMALIZED_SPEED = {"one-hand": 2.4, "two-hand": 3.3, "dagger": 1.7, "ranged": 2.8}   # seconds (ASSUMED)
MELEE_CRIT_MULTIPLIER = 2.0
MELEE_MISS_BY_LEVEL = {0: 5.0, 1: 5.5, 2: 6.0, 3: 8.0}
DODGE_BY_LEVEL = {0: 5.0, 1: 5.5, 2: 6.0, 3: 6.5}
GLANCE_BY_LEVEL = {0: (10.0, 5.0), 1: (20.0, 15.0), 2: (30.0, 25.0), 3: (40.0, 35.0)}   # (% of white hits, % less damage)
CRIT_SUPPRESSION_PER_LEVEL = 1.0
RANGED_MIN_YARDS = 8
ATTACK_CLASSES = frozenset({"Warrior", "Rogue", "Hunter"})   # their abilities are attacks, not spells           # a weapon ability with a longer range uses the ranged weapon
WEAPON_ASSUMPTIONS = (
    "weapon damage from attack power: the weapon's damage + attack power / 14 x its speed (ranged attack power for ranged attacks)",
    "normalized abilities use 2.4 s for one-hand weapons, 3.3 s for two-hand, 1.7 s for daggers, 2.8 s for ranged",
    "'N% weapon damage' applies to the weapon part; the ability's flat amount is added after (as the tooltips read)",
    "melee and ranged crits do 200% (talent crit bonuses raise the extra 100%); ranged crit chance is taken as your melee crit",
    "melee miss 5% against your level, 8% against a boss (less hit); dodge 5% / 6.5%; ranged attacks are not dodged; "
    "you attack from behind (no parry or block); crit 1% lower per level the target is above you",
    "white hits glance: 10% of them for 5% less against your level, 40% for 35% less against a boss",
    "abilities with no cast time and no global cooldown (Heroic Strike, Cleave, Raptor Strike) replace a swing: "
    "their 'per second' is per weapon swing; feral form attacks (Claw, Shred, Maul) are not calculated yet",
)


@dataclass(frozen=True)
class Weapon:
    min: float
    max: float
    speed: float        # seconds
    kind: str           # a NORMALIZED_SPEED key

    @property
    def average(self) -> float:
        return (self.min + self.max) / 2


def _hit_table(target: Target, hit: float, crit: float, ranged: bool, white: bool) -> tuple[float, float, float, float, float]:
    """(miss, dodge, glance chance, glance reduction, crit) in percent, one roll (ASSUMED rules)."""
    d = max(0, min(3, target.level_difference))
    miss = max(0.0, MELEE_MISS_BY_LEVEL[d] - hit)
    dodge = 0.0 if ranged else DODGE_BY_LEVEL[d]
    glance, glance_less = GLANCE_BY_LEVEL[d] if white and not ranged else (0.0, 0.0)
    crit = max(0.0, min(100.0 - miss - dodge - glance, crit - CRIT_SUPPRESSION_PER_LEVEL * d))
    return miss, dodge, glance, glance_less, crit


def weapon_numbers(combat: CombatBook, spells: dict[str, ClassSpell], mods: list[SpellModifier], class_name: str, level: int,
                   ranks: dict[str, int], attack_power: float, ranged_attack_power: float, crit: float, hit: float,
                   target: Target, changes: dict[tuple[str, str], float], main: Weapon | None,
                   ranged: Weapon | None) -> tuple[list[SpellNumbers], list[str]]:
    """Weapon attacks: the white swing with the main-hand weapon, and every class ability with weapon damage.
    Returns (numbers, names not calculated, with the reason in brackets)."""
    bonuses: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for m in mods:
        r = ranks.get(m.spell_id, 0)
        if m.class_name == class_name and r and m.op in BONUS_OPS and applies(m) and r in m.by_rank:
            for name in m.matched:
                bonuses[name][BONUS_OPS[m.op][0]] += m.by_rank[r]

    def row(name, spell_id, weapon, power, pct, flat, normalized, extra, white, is_ranged, time, cost, cost_unit):
        speed = NORMALIZED_SPEED[weapon.kind] if normalized else weapon.speed
        part = power / 14 * speed
        lo = (weapon.min + part) * pct + flat
        hi = (weapon.max + part) * pct + flat
        damage = 1 + extra.get("damage or healing", 0) / 100
        lo, hi = lo * damage, hi * damage
        multiplier = 1 + (MELEE_CRIT_MULTIPLIER - 1) * (1 + extra.get("critical strike bonus", 0) / 100)
        miss, dodge, glance, glance_less, c = _hit_table(target, hit + extra.get("hit chance", 0),
                                                         crit + extra.get("critical strike chance", 0), is_ranged, white)
        average = (lo + hi) / 2
        normal = 100 - miss - dodge - glance - c
        expected = average * (normal + glance * (1 - glance_less / 100) + c * multiplier) / 100
        return SpellNumbers(name, spell_id, "damage", lo, hi, 0.0, 0, 0.0, c, multiplier, 100 - miss - dodge, expected,
                            expected / time if time else None, expected / cost if cost else None, cost_unit,
                            part * pct * damage)

    out, skipped = [], []
    if main is not None:
        out.append(row("Melee swing (main hand)", "", main, attack_power, 1.0, 0.0, False, {}, True, False, main.speed, None, ""))
    for name, spell in sorted(spells.items()):
        base = combat.book.base(spell, level)
        if base.level > level:
            continue
        rows = combat.effects.get(base.spell_id, [])
        weapon_rows = [r for r in rows if _int(r.get("Effect")) in WEAPON_EFFECTS]
        if not weapon_rows:
            continue
        if class_name == "Druid":
            skipped.append(f"{name} (feral form attack)")
            continue
        is_ranged = bool(base.range and base.range > RANGED_MIN_YARDS)
        weapon = ranged if is_ranged else main
        if weapon is None:
            skipped.append(f"{name} (enter your {'ranged' if is_ranged else 'main-hand'} weapon)")
            continue
        pct, flat, normalized = 1.0, 0.0, False
        for r in weapon_rows:
            effect = _int(r.get("Effect"))
            if effect == EFFECT_WEAPON_PERCENT:
                pct *= _amount(r, level, combat.levels) / 100
            else:
                flat += _amount(r, level, combat.levels)
                normalized = normalized or effect == EFFECT_WEAPON_NORMALIZED
        auto = name == "Auto Shot"
        cast = changes.get((name, "cast time"), base.cast_time) or 0
        gcd = changes.get((name, "global cooldown"), base.global_cooldown) or 0
        next_swing = not cast and not gcd   # Heroic Strike, Cleave, Raptor Strike: they replace a swing
        time = weapon.speed if auto or next_swing else max(cast, gcd, 1000) / 1000
        cost = changes.get((name, "cost"), base.cost) if base.cost_unit and not base.cost_unit.startswith("%") else None
        power = ranged_attack_power if is_ranged else attack_power
        out.append(row(name, base.spell_id, weapon, power, pct, flat, normalized and not auto, bonuses.get(name, {}),
                       auto, is_ranged, time, cost, base.cost_unit))
    return out, skipped
