"""What does a talent do? Decode its spell effects from the build's own data.

A talent is a spell with one or more effects. Each effect is a row in SpellEffect;
for a multi-rank talent the value at each rank comes from a scaling curve linked to
the talent's definition (TraitDefinitionEffectPoints -> CurvePoint). This module
turns those rows into `TalentEffect`s the stat engine can use, and says plainly
which effects it cannot:

  stat_percent     aura 137: "+N% to total <stat>". DECODED. The stat is a bit mask
                   in EffectMiscValue_1 (1 strength, 2 agility, 4 stamina, 8 intellect,
                   16 spirit); when that field is empty, EffectMiscValue_0 is a plain
                   stat index (0..4, or -1 for all). The mask was checked against the
                   tooltip of every talent that names a stat (11 talents); the index
                   form appears once (Demonic Embrace's hidden Spirit penalty).
  sheet_add        auras 47 (parry), 49 (dodge), 54 (melee hit), 55 (spell hit) and 290
                   (crit, both melee and spell): "+N percentage points" to a character-sheet
                   figure. DECODED; each was checked against an in-game before/after
                   reading (Deflection, Feral Swiftness, Nature's Reach, Nature's Majesty).
                   Auras 51 (block) and 52 (melee crit) are decoded from the talent texts
                   alone (Shield Specialization, Cruelty, Conviction), with no in-game
                   reading yet. A spell whose text has a duration ("$d") is an ability that
                   lasts a limited time (Holy Shield), not a permanent bonus: not modelled.
                   These add; they are not multiplied. There is no base figure for them in
                   the data, so the sheet reports the talent's contribution only.
  ap_from_stat     aura 598: "Attack Power equal to N% of <stat>" (EffectMiscValue_0 is the
                   stat index; 3 = Intellect was checked in game with Mental Dexterity).
  casting_regen    aura 134: "Allows N% of your Mana regeneration to continue while casting".
                   Kept as the sheet figure casting_regen (a percent, added across talents). The
                   "while casting" in the text IS the figure, so the usual always-on test is not
                   applied to it. It says nothing about how much mana that is: that needs the
                   Spirit-to-regeneration formula, which is not in the data we hold.
  stat_percent     aura 133 too: "Increases your total/maximum Health by N%" (Survivalist,
  (health)         Improved Reincarnation). Kept as a percent of total health, applied after health
                   is built from Stamina. EffectMiscValue_0 is 0 on one and 1 on the other; its
                   meaning is not known (both texts say the same). Rounding NOT checked in game.
  armor_from_stat  aura 268: "Increases your Armor by N% of your <stat>" (Arcane Resilience).
                   EffectMiscValue_0 is the stat index (3 = Intellect, as for aura 598); the text
                   must name armor. Rounded down; NOT checked in game.
  sp_from_stat     aura 174 (spell damage) and 175 (healing): "up to N% of your <stat>".
                   Aura 175 names the stat in EffectMiscValue_0 and aura 174 in
                   EffectMiscValue_1 (3 = Intellect, 4 = Spirit; read against the tooltips
                   of Natural Instinct, Champion of the Light, Spiritual Guidance and Mental
                   Quickness). Aura 174's EffectMiscValue_0 is a school mask; only 126 (every
                   magic school) is modelled. NOT checked in game: how the result is rounded.
  cc_duration      aura 232: "Reduces the duration of <crowd control> effects on you by N%".
                   EffectMiscValue_0 is a SpellMechanic ID, named from the build's own
                   SpellMechanic table (12 stunned, 5 fleeing, 9 silenced, 2 disoriented,
                   7 rooted, 11 ensnared in 1.60.1.70532). DECODED, but only when the
                   tooltip names that crowd control and, if it says "reduces", the values
                   are negative: Iron Will's data says charmed with values +3..+15 (the old
                   Classic resist chance) while its text says Stun and Fear, so it is left
                   unsupported until checked in game. Not a sheet figure: listed beside
                   the sheet. Not checked in game: how two such talents would stack.
  threat           aura 10, damage_done aura 79, damage_taken aura 87: "+/-N% threat / damage
                   done / damage taken" for the schools in EffectMiscValue_0 (a bit mask; bit
                   1 << ID names the school in the build's Resistances table: Physical, Holy,
                   Fire, Nature, Frost, Shadow, Arcane). DECODED only for a passive talent
                   (SpellMisc Attributes_0 & 0x40; checked against 7 passive talents and 9
                   abilities and forms) that applies to you (ImplicitTarget_0 = 1), and only
                   when the phrase in the text that shows the value says the same thing: the
                   right subject (threat, damage done, damage taken), no condition (a
                   duration, "while ...", a stance or form, a weapon, wand or shield), the
                   same schools if it names any, and "increases" or "reduces" matching the
                   sign. Not sheet figures: listed beside the sheet, one line per talent.
                   Not checked in game: how several of them combine.
  ability          any effect of a talent whose spell is not passive (SpellMisc Attributes_0 &
                   0x40 clear): an ability or form. NOT modelled; needs a combat model.
  spell_modifier   auras 107/108: change particular spells (class mask), not the
                   character sheet. Recognised, NOT modelled.
  script           auras 4, 42, 109, 111, 112: implemented in game code; the data
                   holds only numbers and the tooltip text says what they mean (for
                   example Heart of the Wild's Bear Form Stamina). NOT modelled.
  unsupported      anything else, with the reason.

The value of an effect at a rank is the scaling curve's value at that rank (the
curve REPLACES the spell's base points; verified on Heart of the Wild, whose base
points equal its rank-5 value). A multi-rank talent with no scaling data, a curve
with a missing rank, or an unfamiliar scaling operation is reported as unsupported,
never guessed.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from .dataset import Dataset
from .dataset import as_int as _int
from .trees import Node, TreeModel

AURA_STAT_PERCENT = 137
SPELL_MODIFIER_AURAS = frozenset({107, 108})
SCRIPT_AURAS = frozenset({4, 42, 109, 111, 112})

# Additive character-sheet auras -> the sheet figures they raise.
SHEET_ADD_AURAS = {
    47: ("parry",),
    49: ("dodge",),
    51: ("block",),
    52: ("melee_crit",),
    54: ("melee_hit",),
    55: ("spell_hit",),
    290: ("melee_crit", "spell_crit"),
}
AURA_AP_FROM_STAT = 598
AURA_HEALTH_PERCENT = 133
AURA_ARMOR_FROM_STAT = 268
AURA_REGEN_WHILE_CASTING = 134
AURA_MECHANIC_DURATION = 232
# SpellMechanic ID -> words a talent's text uses for it. Checked against the six talents that
# carry aura 232; "movement impairing" covers both rooted and ensnared (Surefooted).
MECHANIC_WORDS = {
    1: ("charm",),
    2: ("disorient",),
    5: ("fear",),
    7: ("movement impairing", "root"),
    9: ("silence",),
    11: ("movement impairing", "snare"),
    12: ("stun",),
}
AURA_SPELL_DAMAGE_FROM_STAT = 174
AURA_HEALING_FROM_STAT = 175
ALL_MAGIC_SCHOOLS = 126
SHEET_FIGURES = ("melee_crit", "spell_crit", "melee_hit", "spell_hit", "dodge", "parry", "block", "casting_regen")

STAT_BITS = {1: "strength", 2: "agility", 4: "stamina", 8: "intellect", 16: "spirit"}
STAT_INDEX = {0: "strength", 1: "agility", 2: "stamina", 3: "intellect", 4: "spirit"}
ALL_STATS = tuple(STAT_BITS.values())

STAT_PERCENT, SPELL_MODIFIER, SCRIPT, UNSUPPORTED = "stat_percent", "spell_modifier", "script", "unsupported"
SHEET_ADD, AP_FROM_STAT = "sheet_add", "ap_from_stat"
SP_FROM_STAT = "sp_from_stat"
ARMOR_FROM_STAT = "armor_from_stat"
CC_DURATION = "cc_duration"
ABILITY = "ability"  # an effect of a spell the game does not flag as passive
THREAT, DAMAGE_DONE, DAMAGE_TAKEN = "threat", "damage_done", "damage_taken"
SCHOOL_KINDS = {10: THREAT, 79: DAMAGE_DONE, 87: DAMAGE_TAKEN}  # aura -> kind; all three carry a school mask
OTHER_KINDS = frozenset({CC_DURATION, THREAT, DAMAGE_DONE, DAMAGE_TAKEN})  # decoded, but not sheet figures
MODELLED_KINDS = frozenset({STAT_PERCENT, SHEET_ADD, AP_FROM_STAT, SP_FROM_STAT, ARMOR_FROM_STAT}) | OTHER_KINDS
SPELL_ATTR0_PASSIVE = 0x40
TARGET_CASTER = 1


@dataclass(frozen=True)
class TalentEffect:
    spell_id: str
    effect_index: int
    aura: int
    kind: str
    stats: tuple[str, ...] = ()
    by_rank: dict[int, float] = field(default_factory=dict)  # rank -> value (a percent for stat_percent)
    note: str = ""
    figures: tuple[str, ...] = ()  # sheet_add: the sheet figures it raises; sp_from_stat: spell_power or healing; cc_duration: the mechanic's name; threat/damage_*: school names (stats holds the source stat for ap_from_stat and sp_from_stat)
    what: str = ""  # OTHER_KINDS: what it applies to, for display ("stunned", "Frost", "all magic schools")
    requires: tuple[tuple[str, bool], ...] = ()  # (CONDITIONS key, must hold): counted only in that situation

    @property
    def modelled(self) -> bool:
        if self.kind in (STAT_PERCENT, AP_FROM_STAT, SP_FROM_STAT, ARMOR_FROM_STAT):
            return bool(self.stats) and bool(self.by_rank)
        return (self.kind == SHEET_ADD or self.kind in OTHER_KINDS) and bool(self.figures) and bool(self.by_rank)


def _float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class EffectBook:
    """The effect rows, scaling links and curves for a set of talents, loaded in
    one pass over the big tables."""

    def __init__(self, ds: Dataset, spell_ids: set[str], definition_ids: set[str]):
        self.effects: dict[str, list[dict]] = defaultdict(list)
        cols = ds.columns("SpellEffect")
        i_spell = cols.index("SpellID")
        for vals in ds.iter_values("SpellEffect"):
            if len(vals) > i_spell and vals[i_spell] in spell_ids:
                self.effects[vals[i_spell]].append(dict(zip(cols, vals)))

        self.text: dict[str, str] = {}  # spell id -> description, to judge whether a bonus is always on
        if ds.has_table("Spell"):
            scols = ds.columns("Spell")
            if "ID" in scols and "Description_lang" in scols:
                i_id, i_desc = scols.index("ID"), scols.index("Description_lang")
                for vals in ds.iter_values("Spell"):
                    if len(vals) > max(i_id, i_desc) and vals[i_id] in spell_ids:
                        self.text[vals[i_id]] = vals[i_desc]

        self.mechanics: dict[int, str] = {}  # SpellMechanic ID -> state name ("stunned")
        if ds.has_table("SpellMechanic") and {"ID", "StateName_lang"} <= set(ds.columns("SpellMechanic")):
            self.mechanics = {_int(r["ID"]): r["StateName_lang"] for r in ds.rows("SpellMechanic")}

        self.schools: dict[int, str] = {}  # Resistances ID -> school name; the mask bit is 1 << ID
        if ds.has_table("Resistances") and {"ID", "Name_lang"} <= set(ds.columns("Resistances")):
            self.schools = {_int(r["ID"]): r["Name_lang"] for r in ds.rows("Resistances")}

        self.passive: dict[str, bool] = {}  # spell id -> SpellMisc says passive
        if ds.has_table("SpellMisc") and {"SpellID", "Attributes_0"} <= set(ds.columns("SpellMisc")):
            mcols = ds.columns("SpellMisc")
            i_sp, i_a0 = mcols.index("SpellID"), mcols.index("Attributes_0")
            for vals in ds.iter_values("SpellMisc"):
                if len(vals) > max(i_sp, i_a0) and vals[i_sp] in spell_ids:
                    self.passive[vals[i_sp]] = bool(_int(vals[i_a0]) & SPELL_ATTR0_PASSIVE)

        self.scaling: dict[tuple[str, int], list[dict]] = defaultdict(list)
        for r in ds.rows("TraitDefinitionEffectPoints"):
            if r["TraitDefinitionID"] in definition_ids:
                self.scaling[(r["TraitDefinitionID"], _int(r["EffectIndex"]))].append(r)

        curve_ids = {r["CurveID"] for rows in self.scaling.values() for r in rows if r["CurveID"] not in ("", "0")}
        self.curves: dict[str, dict[float, float]] = defaultdict(dict)
        if curve_ids:
            c = ds.columns("CurvePoint")
            i_c, i_x, i_y = c.index("CurveID"), c.index("Pos_0"), c.index("Pos_1")
            for vals in ds.iter_values("CurvePoint"):
                if len(vals) > max(i_c, i_x, i_y) and vals[i_c] in curve_ids:
                    self.curves[vals[i_c]][_float(vals[i_x])] = _float(vals[i_y])

    @classmethod
    def for_model(cls, ds: Dataset, model: TreeModel) -> "EffectBook":
        nodes = [n for t in model.trees.values() for n in t.nodes.values() if n.entries]
        return cls(ds, {n.spell_id for n in nodes}, {n.entries[0].definition_id for n in nodes})

    def decode(self, node: Node) -> list[TalentEffect]:
        """Every effect of the talent on `node`, decoded or flagged."""
        if not node.entries:
            return []
        spell, definition = node.spell_id, node.entries[0].definition_id
        out = []
        for row in sorted(self.effects.get(spell, []), key=lambda r: _int(r["EffectIndex"])):
            effect = self._decode_effect(row, node, definition)
            # Second check for the sheet decoders, which judge "always on" from the text: a spell the
            # game flags as not passive (an ability or form) is never a permanent sheet bonus.
            if self.passive.get(spell) is False:
                # An ability or form (SpellMisc): its effects apply when cast or while it is active, so
                # none is a permanent bonus. This is also a second check on the sheet decoders, which
                # judge "always on" from the text.
                effect = TalentEffect(effect.spell_id, effect.effect_index, effect.aura, ABILITY, effect.stats, dict(effect.by_rank),
                                      "an ability or form: applies when cast or while active (needs a combat model)")
            out.append(effect)
        return out

    # ---- internals ----

    def _values(self, node: Node, definition: str, index: int, base: float) -> tuple[dict[int, float], str]:
        rows = self.scaling.get((definition, index), [])
        if not rows:
            if node.max_ranks == 1:
                return {1: base}, ""
            return {}, f"a {node.max_ranks}-rank talent with no scaling data for this effect"
        if len(rows) > 1:
            return {}, "more than one scaling link for this effect"
        link = rows[0]
        if _int(link.get("OperationType")) != 0:
            return {}, f"scaling operation {link.get('OperationType')} is not understood"
        curve = self.curves.get(link["CurveID"], {})
        out: dict[int, float] = {}
        for rank in range(1, node.max_ranks + 1):
            if float(rank) not in curve:
                return {}, f"scaling curve {link['CurveID']} has no value for rank {rank}"
            out[rank] = curve[float(rank)]
        return out, ""

    def _decode_effect(self, row: dict, node: Node, definition: str) -> TalentEffect:
        spell, index, aura = row["SpellID"], _int(row["EffectIndex"]), _int(row["EffectAura"])
        values, values_note = self._values(node, definition, index, _float(row.get("EffectBasePointsF")))

        def make(kind: str, note: str = "", stats: tuple[str, ...] = (), keep_values: bool = True,
                 figures: tuple[str, ...] = (), what: str = "", requires: tuple = ()) -> TalentEffect:
            return TalentEffect(spell, index, aura, kind, stats, dict(values) if keep_values else {},
                                note or values_note, figures, what, requires)

        if aura == AURA_REGEN_WHILE_CASTING:
            if not values:
                return make(UNSUPPORTED, values_note, keep_values=False)
            return make(SHEET_ADD, "", figures=("casting_regen",))
        if aura in SHEET_ADD_AURAS:
            keywords = tuple(w for f in SHEET_ADD_AURAS[aura] for w in FIGURE_WORDS[f])
            reason = condition_in(self.text.get(spell, ""), keywords)
            if reason:
                return make(UNSUPPORTED, f"not always on: {reason}", keep_values=False)
            if not values:
                return make(UNSUPPORTED, values_note, keep_values=False)
            return make(SHEET_ADD, "", figures=SHEET_ADD_AURAS[aura])
        if aura == AURA_HEALTH_PERCENT:
            reason = condition_in(self.text.get(spell, ""), ("health",))
            if reason:
                return make(UNSUPPORTED, f"not always on: {reason}", keep_values=False)
            if "health" not in self.text.get(spell, "").lower():
                return make(UNSUPPORTED, "aura 133 but the text does not mention health", keep_values=False)
            if not values:
                return make(UNSUPPORTED, values_note, keep_values=False)
            return make(STAT_PERCENT, "", ("health",))
        if aura == AURA_ARMOR_FROM_STAT:
            misc0 = _int(row.get("EffectMiscValue_0"), -2)
            text = self.text.get(spell, "").lower()
            if misc0 not in STAT_INDEX:
                return make(UNSUPPORTED, f"unknown source stat {row.get('EffectMiscValue_0')!r}", keep_values=False)
            if "armor" not in text or STAT_INDEX[misc0] not in text:
                return make(UNSUPPORTED, f"armor from {STAT_INDEX[misc0]}, but the text does not say so", keep_values=False)
            reason = condition_in(self.text.get(spell, ""), ("armor",))
            if reason:
                return make(UNSUPPORTED, f"not always on: {reason}", keep_values=False)
            if not values:
                return make(UNSUPPORTED, values_note, keep_values=False)
            return make(ARMOR_FROM_STAT, "", (STAT_INDEX[misc0],))
        if aura == AURA_AP_FROM_STAT:
            misc0 = _int(row.get("EffectMiscValue_0"), -2)
            if misc0 not in STAT_INDEX:
                return make(UNSUPPORTED, f"unknown source stat {row.get('EffectMiscValue_0')!r}", keep_values=False)
            if not values:
                return make(UNSUPPORTED, values_note, keep_values=False)
            return make(AP_FROM_STAT, "", (STAT_INDEX[misc0],))

        if aura in (AURA_SPELL_DAMAGE_FROM_STAT, AURA_HEALING_FROM_STAT):
            damage = aura == AURA_SPELL_DAMAGE_FROM_STAT
            if damage and _int(row.get("EffectMiscValue_0")) != ALL_MAGIC_SCHOOLS:
                return make(UNSUPPORTED, f"spell damage for some schools only (mask {row.get('EffectMiscValue_0')!r}); not modelled", keep_values=False)
            source = _int(row.get("EffectMiscValue_1" if damage else "EffectMiscValue_0"), -2)
            if source not in STAT_INDEX:
                return make(UNSUPPORTED, f"unknown source stat {row.get('EffectMiscValue_1' if damage else 'EffectMiscValue_0')!r}", keep_values=False)
            if not values:
                return make(UNSUPPORTED, values_note, keep_values=False)
            return make(SP_FROM_STAT, "", (STAT_INDEX[source],), figures=("spell_power" if damage else "healing",))

        if aura == AURA_STAT_PERCENT:
            stats, why = decode_stats(_int(row.get("EffectMiscValue_0")), _int(row.get("EffectMiscValue_1")))
            if why:
                return make(UNSUPPORTED, why, keep_values=False)
            if not values:
                return make(UNSUPPORTED, values_note, stats, keep_values=False)
            return make(STAT_PERCENT, "", stats)
        if aura == AURA_MECHANIC_DURATION:
            return self._decode_mechanic_duration(row, make, values, values_note)
        if aura in SCHOOL_KINDS:
            return self._decode_school_percent(row, index, SCHOOL_KINDS[aura], make, values, values_note)
        if aura in SPELL_MODIFIER_AURAS:
            return make(SPELL_MODIFIER, "changes particular spells, not the character sheet")
        if aura in SCRIPT_AURAS:
            return make(SCRIPT, "implemented in game code; see the tooltip text")
        if aura == 0:
            return make(UNSUPPORTED, "no aura (an instant effect)")
        return make(UNSUPPORTED, f"aura {aura} is not decoded yet")

    def _decode_school_percent(self, row: dict, index: int, kind: str, make, values: dict[int, float], values_note: str) -> TalentEffect:
        def refuse(why: str) -> TalentEffect:
            return make(UNSUPPORTED, f"{KIND_LABEL[kind]}: {why}", keep_values=bool(values))

        spell = row["SpellID"]
        if spell not in self.passive:
            return refuse("cannot tell whether it is always on (SpellMisc is not in this build)")
        if _int(row.get("ImplicitTarget_0"), -1) != TARGET_CASTER:
            return refuse(f"applies to target type {row.get('ImplicitTarget_0')!r}, not to you")
        if not values:
            return refuse(values_note)
        schools, why = decode_schools(_int(row.get("EffectMiscValue_0")), self.schools)
        if why:
            return refuse(why)
        found = effect_phrase(self.text.get(spell, ""), index)
        if found is None:
            return refuse("the talent text does not show this effect's value")
        phrase, segment, verb, tail = found
        problems = phrase_problems(kind, phrase, segment, verb, schools, set(self.schools.values()), values, tail)
        if problems:
            return refuse("; ".join(problems))
        _, requires, scope, _ = situation(segment, tail)
        label = schools_label(schools, set(self.schools.values()))
        return make(kind, "", figures=schools, what=f"{scope}, {label}" if scope else label, requires=requires)

    def _decode_mechanic_duration(self, row: dict, make, values: dict[int, float], values_note: str) -> TalentEffect:
        mechanic = _int(row.get("EffectMiscValue_0"), -1)
        name = self.mechanics.get(mechanic, f"mechanic {mechanic}")
        if not values:
            return make(UNSUPPORTED, f"{name} duration: {values_note}", keep_values=False)
        words = MECHANIC_WORDS.get(mechanic)
        if words is None:
            return make(UNSUPPORTED, f"changes the duration of {name} effects; no tooltip check for this kind of effect yet")
        text = self.text.get(row["SpellID"], "").lower()
        problems = []
        if not any(w in text for w in words):
            problems.append(f"the data says {name} effects but the talent text does not mention them")
        if "reduc" in text and any(v > 0 for v in values.values()):
            problems.append("the values are positive, which would lengthen the effect, but the text says it reduces")
        if problems:
            return make(UNSUPPORTED, "; ".join(problems) + " (needs an in-game check)")
        return make(CC_DURATION, "", figures=(name,), what=name)


def decode_stats(misc0: int, misc1: int) -> tuple[tuple[str, ...], str]:
    """(stats, problem). Exactly one of the two is meaningful."""
    if misc1:
        leftover = misc1 & ~sum(STAT_BITS)
        bits = tuple(STAT_BITS[b] for b in STAT_BITS if misc1 & b)
        if leftover or not bits:
            return (), f"unknown stat mask {misc1}"
        return bits, ""
    if misc0 == -1:
        return ALL_STATS, ""
    if misc0 in (1, 2, 3, 4):
        return (STAT_INDEX[misc0],), ""
    if misc0 == 0:
        return (), "the stat is not specified (both stat fields are 0)"
    return (), f"unknown stat index {misc0}"


_DURATION = re.compile(r"\$\d*d\b")          # $d, $24394d: the spell's duration
_WHILE = re.compile(r"\bwhile\b((?:(?! by )[^.,;])*)", re.I)
_OFF_HAND = re.compile(r"off[- ]?hand", re.I)


_CLAUSES = re.compile(r",? and |;|\. ")
FIGURE_WORDS = {"parry": ("parry",), "dodge": ("dodge",), "block": ("block",), "melee_hit": ("hit",), "spell_hit": ("hit",),
                "melee_crit": ("crit",), "spell_crit": ("crit",)}


def condition_in(description: str, keywords: tuple[str, ...] = ()) -> str:
    """Why a talent's text says its bonus is NOT always on, or "" if nothing suggests that.

    A duration anywhere in the text means an activated ability (Holy Shield, Intimidation), so the
    whole text counts. A "while ..." clause (a form or state) or an off-hand restriction only counts
    when it is in the part of the text that talks about the figure: Feral Swiftness says "movement
    speed while in Cat Form by 30%, and increases your chance to Dodge by 4%", and the dodge is not
    limited to Cat Form (checked in game). `keywords` pick that part (a clause that contains one of
    them); with none given, or none found, the whole text is examined."""
    if _DURATION.search(description):
        return "an ability that lasts a limited time"
    clauses = [c for c in _CLAUSES.split(description) if c.strip()]
    relevant = [c for c in clauses if any(k in c.lower() for k in keywords)] if keywords else []
    scope = " ".join(relevant or clauses)
    m = _WHILE.search(scope)
    if m:
        return "only while" + m.group(1).rstrip()
    if _OFF_HAND.search(scope):
        return "only for off-hand attacks"
    return ""


_PLACEHOLDER = re.compile(r"\$[sm](\d)")
_SENTENCES = re.compile(r"(?<=[.!?])\s+")
_VERB = re.compile(r"\b(increas|improv|reduc|decreas|lower)\w*", re.I)
_EQUIPMENT = re.compile(r"\b(wands?|weapons?|shields?|daggers?|maces?|swords?|axes?|fist weapons?|staff|staves|polearms?|bows?|guns?|crossbows?|thrown|off-?hand)\b", re.I)
_STANCE_OR_FORM = re.compile(r"\bin [\w ]*?\b(stance|form)\b", re.I)
_TAKEN = re.compile(r"\btak(e|en)\b", re.I)
KIND_LABEL = {THREAT: "threat", DAMAGE_DONE: "damage done", DAMAGE_TAKEN: "damage taken", CC_DURATION: "duration"}


def describe_other(kind: str, what: str) -> str:
    """How an OTHER_KINDS effect reads beside the sheet ("Threat (Frost)")."""
    if kind == CC_DURATION:
        return f"Duration of {what} effects on you"
    return f"{KIND_LABEL[kind].capitalize()} ({what})"


def decode_schools(mask: int, names: dict[int, str]) -> tuple[tuple[str, ...], str]:
    """(school names, problem) for a school bit mask; names maps Resistances ID -> name."""
    if not names:
        return (), "no school names (Resistances is not in this build)"
    if mask <= 0:
        return (), f"no school in mask {mask}"
    known = sum(1 << i for i in names)
    if mask & ~known:
        return (), f"unknown school bits in mask {mask}"
    return tuple(names[i] for i in sorted(names) if mask & (1 << i)), ""


def schools_label(schools: tuple[str, ...], all_names: set[str]) -> str:
    """'all schools', 'all magic schools' (everything but Physical) or the names."""
    if set(schools) == all_names:
        return "all schools"
    if "Physical" in all_names and set(schools) == all_names - {"Physical"}:
        return "all magic schools"
    return ", ".join(schools)


def effect_phrase(text: str, index: int) -> tuple[str, str, str, str] | None:
    """The words in the description that go with effect `index` (shown as $s<index+1> or $m<index+1>).

    Returns (phrase, segment, verb, tail): the segment is the sentence from the previous other
    effect's value to the next one; the phrase runs from the last verb before this value up
    to it ("reduces the threat caused by your Frost spells by "); the tail is what follows the
    sentence's last value when that value is another effect's ("" otherwise). None if the text
    does not show this effect's value."""
    for sentence in _SENTENCES.split(text):
        marks = list(_PLACEHOLDER.finditer(sentence))
        mine = [m for m in marks if int(m.group(1)) == index + 1]
        if not mine:
            continue
        m = mine[0]
        others = [p for p in marks if int(p.group(1)) != index + 1]
        start = max((p.end() for p in others if p.start() < m.start()), default=0)
        end = min((p.start() for p in others if p.start() > m.end()), default=len(sentence))
        lead = sentence[start:m.start()]
        verbs = list(_VERB.finditer(lead))
        phrase = lead[verbs[-1].start():] if verbs else lead
        tail = sentence[marks[-1].end():] if marks[-1].start() > m.end() else ""
        return phrase, sentence[start:end], (verbs[-1].group(1).lower() if verbs else ""), tail
    return None


# Situations a talent's text makes a bonus depend on, each tied to an input the person sets:
# key -> (label when it must hold, label when it must not).
CONDITIONS = {
    "shield": ("Wearing a shield", "Not wearing a shield"),
    "two_hand": ("Two-handed weapon equipped", "No two-handed weapon"),
    "defensive_stance": ("In Defensive Stance", "Not in Defensive Stance"),
    "rockbiter": ("Rockbiter Weapon active", "Rockbiter Weapon not active"),
}
# The exact wording that maps to each one (checked against the talents that use it).
_CONDITION_PHRASES = [
    (re.compile(r"\s*while a shield is equipped", re.I), "shield", True),           # Bastion, Defiance
    (re.compile(r"\s*while a two-handed melee weapon is equipped", re.I), "two_hand", True),   # Two-Handed Weapon Specialization (Paladin)
    (re.compile(r"\s*in defensive stance", re.I), "defensive_stance", True),         # Defiance
    (re.compile(r"\s*while rockbiter weapon is not active", re.I), "rockbiter", False),   # Spirit Weapons
    (re.compile(r"\s*while rockbiter weapon is active", re.I), "rockbiter", True),
]
# "Damage with X": which attacks a bonus covers, not a situation; shown in the label.
_SCOPE_PHRASES = [
    (re.compile(r"\s*with (?:your )?wands", re.I), "with wands"),
    (re.compile(r"\s*with ranged weapons", re.I), "with ranged weapons"),
    (re.compile(r"\s*with two-handed melee weapons", re.I), "with two-handed melee weapons"),
    (re.compile(r"\s*with one-handed melee weapons", re.I), "with one-handed melee weapons"),
]


def describe_requirement(requires: tuple[tuple[str, bool], ...]) -> str:
    return " and ".join(CONDITIONS[key][0 if wanted else 1] for key, wanted in requires)


def _has_condition(text: str) -> bool:
    return bool(condition_in(text) or _STANCE_OR_FORM.search(text) or _EQUIPMENT.search(text)
                or any(rx.search(text) for rx, _, _ in _CONDITION_PHRASES))


def situation(segment: str, tail: str = "") -> tuple[str, tuple[tuple[str, bool], ...], str, list[str]]:
    """(text left after known conditions and scopes are taken out, requirements, scope, problems).

    A condition at the end of a sentence ("..., and reduces threat by $m2% while a two-handed
    weapon is equipped") also covers the earlier values of that sentence, unless their own part
    of the text already has a condition."""
    text = segment if _has_condition(segment) or not tail else segment + " " + tail
    requires: dict[str, bool] = {}
    problems = []
    for rx, key, wanted in _CONDITION_PHRASES:
        if rx.search(text):
            if requires.get(key, wanted) != wanted:
                problems.append(f"the text both requires and excludes '{CONDITIONS[key][0]}'")
            requires[key] = wanted
            text = rx.sub("", text)
    scopes = []
    for rx, label in _SCOPE_PHRASES:
        if rx.search(text):
            scopes.append(label)
            text = rx.sub("", text)
    if len(scopes) > 1:
        problems.append("the text names more than one kind of weapon")
    return text, tuple(sorted(requires.items())), (scopes[0] if scopes else ""), problems


def phrase_problems(kind: str, phrase: str, segment: str, verb: str, schools: tuple[str, ...],
                    all_names: set[str], values: dict[int, float], tail: str = "") -> list[str]:
    """Why the text does not back this reading of the data (empty: it does)."""
    out = []
    low = phrase.lower()
    if kind == THREAT and "threat" not in low:
        out.append("the text next to the value is not about threat")
    if kind == DAMAGE_DONE and ("damage" not in low or _TAKEN.search(low)):
        out.append("the text next to the value is not about damage done")
    if kind == DAMAGE_TAKEN and ("damage" not in low or not _TAKEN.search(low)):
        out.append("the text next to the value is not about damage taken")
    segment, _, _, situation_problems = situation(segment, tail)
    out.extend(situation_problems)
    reason = condition_in(segment)
    if reason:
        out.append(f"not always on: {reason}")
    elif _STANCE_OR_FORM.search(segment):
        out.append("not always on: only " + _STANCE_OR_FORM.search(segment).group(0))
    elif _EQUIPMENT.search(segment):
        out.append(f"only with particular equipment ({_EQUIPMENT.search(segment).group(1).lower()}); not modelled")
    named = {n for n in all_names if re.search(rf"\b{re.escape(n)}\b", phrase, re.I)}
    if named and named != set(schools):
        out.append(f"the text names {', '.join(sorted(named))} but the data says {', '.join(schools)}")
    if not verb:
        out.append("the text does not say whether it increases or reduces")
    elif verb in ("increas", "improv") and any(v < 0 for v in values.values()):
        out.append("the text says it increases but the values are negative")
    elif verb in ("reduc", "decreas", "lower") and any(v > 0 for v in values.values()):
        out.append("the text says it reduces but the values are positive")
    return out


def render_tooltip(text: str, effects: list[TalentEffect], rank: int) -> tuple[str, bool]:
    """The spell's description with each $sN / $mN replaced by that effect's value at
    `rank`. Returns (text, complete); complete is False if anything was left that this
    cannot render (other $ expressions, or an effect with no value at this rank).

    Descriptions number effects from 1, so $s2 is effect index 1."""
    by_index = {e.effect_index: e for e in effects}
    complete = True

    def sub(match: re.Match) -> str:
        nonlocal complete
        effect = by_index.get(int(match.group(1)) - 1)
        if effect is None or rank not in effect.by_rank:
            complete = False
            return match.group(0)
        value = abs(effect.by_rank[rank])
        return str(int(value)) if value == int(value) else f"{value:g}"

    out = " ".join(_PLACEHOLDER.sub(sub, text).split())
    return out, complete and "$" not in out
