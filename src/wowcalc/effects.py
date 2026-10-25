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
  sp_from_stat     aura 174 (spell damage) and 175 (healing): "up to N% of your <stat>".
                   Aura 175 names the stat in EffectMiscValue_0 and aura 174 in
                   EffectMiscValue_1 (3 = Intellect, 4 = Spirit; read against the tooltips
                   of Natural Instinct, Champion of the Light, Spiritual Guidance and Mental
                   Quickness). Aura 174's EffectMiscValue_0 is a school mask; only 126 (every
                   magic school) is modelled. NOT checked in game: how the result is rounded.
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
AURA_REGEN_WHILE_CASTING = 134
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
MODELLED_KINDS = frozenset({STAT_PERCENT, SHEET_ADD, AP_FROM_STAT, SP_FROM_STAT})


@dataclass(frozen=True)
class TalentEffect:
    spell_id: str
    effect_index: int
    aura: int
    kind: str
    stats: tuple[str, ...] = ()
    by_rank: dict[int, float] = field(default_factory=dict)  # rank -> value (a percent for stat_percent)
    note: str = ""
    figures: tuple[str, ...] = ()  # sheet_add: the sheet figures it raises; sp_from_stat: spell_power or healing (stats holds the source stat for ap_from_stat and sp_from_stat)

    @property
    def modelled(self) -> bool:
        if self.kind in (STAT_PERCENT, AP_FROM_STAT, SP_FROM_STAT):
            return bool(self.stats) and bool(self.by_rank)
        return self.kind == SHEET_ADD and bool(self.figures) and bool(self.by_rank)


def _int(value, default=0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


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
            out.append(self._decode_effect(row, node, definition))
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
                 figures: tuple[str, ...] = ()) -> TalentEffect:
            return TalentEffect(spell, index, aura, kind, stats, dict(values) if keep_values else {},
                                note or values_note, figures)

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
        if aura in SPELL_MODIFIER_AURAS:
            return make(SPELL_MODIFIER, "changes particular spells, not the character sheet")
        if aura in SCRIPT_AURAS:
            return make(SCRIPT, "implemented in game code; see the tooltip text")
        if aura == 0:
            return make(UNSUPPORTED, "no aura (an instant effect)")
        return make(UNSUPPORTED, f"aura {aura} is not decoded yet")


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
