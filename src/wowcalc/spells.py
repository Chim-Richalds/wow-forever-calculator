"""The spell model, phase 1 (docs/spell-model-plan.md): each class's spells, and which of them
each talent's spell modifier (aura 107 flat, 108 percent) reaches. Values do not change yet; this
module builds the catalogue, resolves every modifier and audits it against the talent's text.

    python -m wowcalc.spells audit [--class Mage] [--out docs/spell-modifier-audit.tsv]

Which spells: the modifier's 128-bit class mask (EffectSpellClassMask_0..3) is matched against each
spell's mask in SpellClassOptions, within the talent spell's own family (SpellClassSet). Only spells
the class can have are considered: trainer spells (SkillLineAbility by class) and abilities that
talents grant (talent spells that are not passive). Without that limit the masks also match NPC
spells that share the family.

What is changed: EffectMiscValue_0, the "modifier op". OP_WORDS holds, for each op whose meaning the
talents' texts establish, the words the text uses for it. The audit checks every modifier against
them; an op with no entry is reported, never guessed.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .dataset import Dataset
from .dataset import as_int as _int
from .effects import SPELL_MODIFIER, EffectBook
from .trees import TreeModel

CLASS_BITS = {"Warrior": 1, "Paladin": 2, "Hunter": 4, "Rogue": 8, "Priest": 16, "Shaman": 64, "Mage": 128,
              "Warlock": 256, "Druid": 1024}
AURA_FLAT, AURA_PERCENT = 107, 108

# Modifier op -> (name, words a talent's text uses for it). Each is backed by talents whose text says so
# (examples in brackets); an op missing here is reported as not established.
OP_WORDS: dict[int, tuple[str, tuple[str, ...]]] = {
    0: ("damage or healing", ("damage", "healing", "heal")),               # Improved Cone of Cold
    1: ("duration", ("duration", "lasts", "last ")),                        # Permafrost
    5: ("range", ("range",)),                                               # Arctic Reach
    6: ("radius", ("radius",)),                                             # Arctic Reach
    7: ("critical strike chance", ("critical strike chance", "chance to critically", "crit")),   # Critical Mass
    10: ("cast time", ("cast time", "casting time")),                       # Improved Frostbolt
    11: ("cooldown", ("cooldown",)),                                        # Improved Frost Nova
    14: ("cost", ("cost",)),                                                # Frost Channeling
    15: ("critical strike bonus", ("critical strike damage", "critical strike bonus", "critical damage", "critical strike healing")),  # Ice Shards
    21: ("global cooldown", ("global cooldown",)),                          # Improved Slam
    2: ("threat", ("threat",)),                                             # Iron Creed, Arcane Subtlety (5 of 5)
    4: ("charges", ("charges", "next")),                                    # Improved Inner Fire, Demonic Brand
    9: ("pushback", ("not lose casting time",)),                            # Burning Soul, Spiritual Focus (10 of 10)
    16: ("hit chance", ("chance to hit",)),                                 # Holy Precision, Survival Tactics (4 of 4)
    18: ("proc chance", ("chance",)),                                       # Deadly Aspects, Frenzy, Improved Poisons
    22: ("periodic damage or healing", ("periodic", "damage", "healing", "effect")),   # Genesis, Improved Rejuvenation
}
# Ops that change a spell's own effect amounts (Frost Armor's armor, Inner Fire's armor, a pet's
# health). Their wording follows the spell, so they are valued with the spells' effects (phase 4).
EFFECT_AMOUNT_OPS = frozenset({3, 8, 12})

SCHOOL_WORDS = {"Physical": "physical", "Holy": "holy", "Fire": "fire", "Nature": "nature", "Frost": "frost",
                "Shadow": "shadow", "Arcane": "arcane"}
# Placeholders a description uses for an effect's value: $s1, $m2, $/1000;S1, ${$m1/-1000}.
_VALUE = re.compile(r"\$(?:/\d+;|\{\$)?[sSmM](\d)")
_SENTENCES = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class ClassSpell:
    name: str
    spell_ids: tuple[str, ...]   # every rank
    family: str                  # SpellClassSet
    masks: tuple[tuple[int, int, int, int], ...]   # per rank
    schools: tuple[str, ...]     # from SpellMisc.SchoolMask
    source: str                  # "trainer" or "talent"


@dataclass(frozen=True)
class SpellModifier:
    class_name: str
    talent: str
    spell_id: str
    effect_index: int
    op: int
    flat: bool                   # aura 107 (flat) or 108 (percent)
    by_rank: dict[int, float]
    matched: tuple[str, ...]     # catalogue spell names the mask reaches
    named: tuple[str, ...]       # catalogue spell names the text mentions
    schools_named: tuple[str, ...]
    problems: tuple[str, ...] = ()

    @property
    def op_name(self) -> str:
        return OP_WORDS[self.op][0] if self.op in OP_WORDS else f"op {self.op}"

    @property
    def clean(self) -> bool:
        return not self.problems


def _rows(ds: Dataset, table: str, columns: set[str]) -> list[dict]:
    """The table's rows, or none if the build lacks the table or these columns."""
    if not ds.has_table(table) or not columns <= set(ds.columns(table)):
        return []
    return ds.rows(table)


def _u32(value) -> int:
    return _int(value) & 0xFFFFFFFF


def _schools(mask: int, names: dict[int, str]) -> tuple[str, ...]:
    return tuple(names[i] for i in sorted(names) if mask & (1 << i))


@dataclass
class Catalogue:
    """The spells each class can have, grouped by name."""
    by_class: dict[str, dict[str, ClassSpell]] = field(default_factory=dict)
    family_of: dict[str, str] = field(default_factory=dict)   # spell id -> SpellClassSet
    family_spells: list[tuple[str, str, tuple[int, int, int, int]]] = field(default_factory=list)   # every (id, family, mask)
    names: dict[str, str] = field(default_factory=dict)

    @classmethod
    def build(cls, ds: Dataset, model: TreeModel, book: EffectBook) -> "Catalogue":
        names = {r["ID"]: r["Name_lang"] for r in _rows(ds, "SpellName", {"ID", "Name_lang"})}
        options = {}
        for r in _rows(ds, "SpellClassOptions", {"SpellID", "SpellClassSet", "SpellClassMask_0", "SpellClassMask_1",
                                                  "SpellClassMask_2", "SpellClassMask_3"}):
            options[r["SpellID"]] = (r["SpellClassSet"], tuple(_u32(r[f"SpellClassMask_{i}"]) for i in range(4)))
        school_names = book.schools
        school_mask = {r["SpellID"]: _int(r.get("SchoolMask")) for r in _rows(ds, "SpellMisc", {"SpellID"})}
        learnable: dict[str, dict[str, str]] = defaultdict(dict)   # class -> spell id -> source
        for r in _rows(ds, "SkillLineAbility", {"Spell", "ClassMask"}):
            bits = _int(r.get("ClassMask"))
            for class_name, bit in CLASS_BITS.items():
                if bits & bit:
                    learnable[class_name][r["Spell"]] = "trainer"
        for tree in model.trees.values():
            for node in tree.nodes.values():
                for entry in node.entries:
                    if book.passive.get(entry.spell_id) is False:
                        learnable[tree.class_name].setdefault(entry.spell_id, "talent")
        out = cls(family_of={sid: fam for sid, (fam, _) in options.items()},
                  family_spells=[(sid, fam, mask) for sid, (fam, mask) in options.items()], names=names)
        for class_name, spells in learnable.items():
            grouped: dict[str, list[str]] = defaultdict(list)
            for sid in spells:
                if sid in options and sid in names:
                    grouped[names[sid]].append(sid)
            out.by_class[class_name] = {
                name: ClassSpell(name, tuple(sorted(ids, key=_int)), options[ids[0]][0], tuple(options[i][1] for i in ids),
                                 tuple(sorted({s for i in ids for s in _schools(school_mask.get(i, 0), school_names)})),
                                 "talent" if all(spells[i] == "talent" for i in ids) else "trainer")
                for name, ids in grouped.items()}
        return out

    def reached_elsewhere(self, family: str, mask: tuple[int, int, int, int]) -> tuple[str, ...]:
        """Spells of the family the mask reaches that the character does not cast itself (pet
        abilities, poisons, totem and weapon effects, debuffs)."""
        return tuple(sorted({self.names.get(sid, sid) for sid, fam, m in self.family_spells
                             if fam == family and any(m[i] & mask[i] for i in range(4))}))

    def reached(self, class_name: str, family: str, mask: tuple[int, int, int, int]) -> tuple[str, ...]:
        return tuple(sorted(s.name for s in self.by_class.get(class_name, {}).values()
                            if s.family == family and any(any(m[i] & mask[i] for i in range(4)) for m in s.masks)))


def effect_text(text: str, index: int) -> str:
    """The part of a description that goes with effect `index`: from the previous value shown in its
    sentence (or the sentence start) to the next one. "" if the text does not show its value."""
    for sentence in _SENTENCES.split(text):
        marks = list(_VALUE.finditer(sentence))
        mine = [m for m in marks if int(m.group(1)) == index + 1]
        if not mine:
            continue
        m = mine[0]
        start = max((p.end() for p in marks if p.end() <= m.start()), default=0)
        end = min((p.start() for p in marks if p.start() >= m.end()), default=len(sentence))
        return sentence[start:end]
    return ""


def resolve(ds: Dataset, model: TreeModel, book: EffectBook, catalogue: Catalogue | None = None) -> list[SpellModifier]:
    """Every spell modifier on a placed talent, with the spells it reaches and what the text says."""
    catalogue = catalogue or Catalogue.build(ds, model, book)
    spell_names = {r["ID"]: r["Name_lang"] for r in _rows(ds, "SpellName", {"ID", "Name_lang"})}
    out = []
    for tree in sorted(model.trees.values(), key=lambda t: t.class_name):
        spells = catalogue.by_class.get(tree.class_name, {})
        for node in sorted(tree.nodes.values(), key=lambda n: (n.sub_tree, n.y, n.x)):
            if not node.entries:
                continue
            mods = [e for e in book.decode(node) if e.kind == SPELL_MODIFIER]
            if not mods:
                continue
            family = catalogue.family_of.get(node.spell_id, "")
            text = book.text.get(node.spell_id, "")
            whole = text.lower()
            # Texts often describe several modifiers in one clause and show one value ("the range of X
            # and the radius of Y by $s1%"), so which spells a talent covers is checked for the talent
            # as a whole: every spell its text names should be reached by one of its modifiers, and every
            # spell a modifier reaches should be named somewhere in its text (or covered by a school).
            talent_named = {n for n in spells if re.search(rf"\b{re.escape(n.lower())}\b", whole)}
            reached_by = {}
            for e in mods:
                row = next(r for r in book.effects[node.spell_id] if _int(r["EffectIndex"]) == e.effect_index)
                mask = tuple(_u32(row.get(f"EffectSpellClassMask_{i}")) for i in range(4))
                reached_by[e.effect_index] = (row, catalogue.reached(tree.class_name, family, mask), mask)
            talent_reached = {n for _, reached, _ in reached_by.values() for n in reached}
            for e in mods:
                row, matched, mask = reached_by[e.effect_index]
                part = effect_text(text, e.effect_index).lower()
                checked = part or whole          # an effect whose value the text does not show is checked against all of it
                named = tuple(sorted(n for n in spells if re.search(rf"\b{re.escape(n.lower())}\b", checked)))
                schools = tuple(s for s, w in SCHOOL_WORDS.items() if re.search(rf"\b{w}\b", checked))
                op = _int(row.get("EffectMiscValue_0"), -1)
                problems = []
                if op in EFFECT_AMOUNT_OPS:
                    problems.append(f"op {op}: changes the spell's own effect amounts; valued with the spells' effects (phase 4)")
                elif op not in OP_WORDS:
                    problems.append(f"op {op}: what it changes is not established from talent texts yet")
                elif not any(w in checked for w in OP_WORDS[op][1]):
                    problems.append(f"op {op} ({OP_WORDS[op][0]}), but the text does not say so")
                if not e.by_rank:
                    problems.append(f"no value per rank in the data ({e.note or 'no scaling data'})")
                if not family:
                    problems.append("the talent spell has no spell family (SpellClassOptions)")
                elif not matched:
                    other = catalogue.reached_elsewhere(family, mask)
                    problems.append("reaches no spell the character casts itself" + (
                        f"; it reaches {', '.join(other[:5])}{' ...' if len(other) > 5 else ''} (pet abilities, poisons, totem or weapon effects: modelled later)"
                        if other else "; its mask matches no spell at all"))
                missing = sorted(set(named) - talent_reached)
                if missing:
                    problems.append("the text names " + ", ".join(missing) + " but no modifier of the talent reaches it")
                if schools and matched:
                    off = [n for n in matched if not set(spells[n].schools) & set(schools)]
                    if off:
                        problems.append(f"the text says {'/'.join(schools)} spells, but the mask also reaches " + ", ".join(off))
                elif talent_named and matched:
                    extra = sorted(set(matched) - talent_named)
                    if extra:
                        problems.append("the mask also reaches " + ", ".join(extra) + ", which the text does not name")
                out.append(SpellModifier(tree.class_name, spell_names.get(node.spell_id, node.spell_id), node.spell_id,
                                         e.effect_index, op, e.aura == AURA_FLAT, dict(e.by_rank), matched, named, schools,
                                         tuple(problems)))
    return out


# ---- phase 2: what the talents do to each spell's cost, cast time, cooldown, duration and range

# Op -> the property it changes. Times are milliseconds, range yards, costs in the table's units
# (rage in tenths: Heroic Strike 150 = 15 rage, and Improved Heroic Strike -10 = -1 rage).
PROPERTY_OPS = {14: "cost", 10: "cast time", 11: "cooldown", 21: "global cooldown", 1: "duration", 5: "range"}
POWER_TYPES = {0: ("mana", 1), 1: ("rage", 10), 3: ("energy", 1), 2: ("focus", 1), 6: ("runic power", 10)}


@dataclass(frozen=True)
class SpellBase:
    """One spell's properties at the highest rank the level allows. None: the spell has none."""
    name: str
    spell_id: str
    level: int
    cost: float | None = None
    cost_unit: str = ""          # "mana", "rage", "energy", or "% of base mana"
    cast_time: float | None = None       # milliseconds
    cooldown: float | None = None
    global_cooldown: float | None = None
    duration: float | None = None
    range: float | None = None           # yards


@dataclass(frozen=True)
class SpellChange:
    spell: str
    prop: str                    # a PROPERTY_OPS value
    base: float
    value: float
    unit: str
    by: tuple[tuple[str, str], ...]   # (talent, what it does: "-0.5 s", "-15%")


class SpellBook:
    """Base properties of the class spells, read once."""

    def __init__(self, ds: Dataset):
        self.levels = {r["SpellID"]: _int(r.get("SpellLevel")) or _int(r.get("BaseLevel")) for r in _rows(ds, "SpellLevels", {"SpellID"})}
        self.power: dict[str, dict] = {}
        for r in _rows(ds, "SpellPower", {"SpellID", "ManaCost"}):
            if r["SpellID"] not in self.power or _int(r.get("OrderIndex")) < _int(self.power[r["SpellID"]].get("OrderIndex")):
                self.power[r["SpellID"]] = r
        self.misc = {r["SpellID"]: r for r in _rows(ds, "SpellMisc", {"SpellID"})}
        self.cast = {r["ID"]: _int(r["Base"]) for r in _rows(ds, "SpellCastTimes", {"ID", "Base"})}
        self.cooldowns = {r["SpellID"]: r for r in _rows(ds, "SpellCooldowns", {"SpellID"})}
        self.durations = {r["ID"]: _int(r["Duration"]) for r in _rows(ds, "SpellDuration", {"ID", "Duration"})}
        self.ranges = {r["ID"]: float(r["RangeMax_0"] or 0) for r in _rows(ds, "SpellRange", {"ID", "RangeMax_0"})}

    def base(self, spell: ClassSpell, level: int) -> SpellBase:
        ranks = [(self.levels.get(sid, 0), _int(sid), sid) for sid in spell.spell_ids]
        usable = [r for r in ranks if r[0] <= level] or [min(ranks)]
        lv, _, sid = max(usable)
        misc = self.misc.get(sid, {})
        cost, unit = None, ""
        pw = self.power.get(sid)
        if pw is not None:
            if _int(pw.get("PowerCostPct")):
                cost, unit = float(pw["PowerCostPct"]), "% of base mana"
            elif _int(pw.get("ManaCost")):
                name, scale = POWER_TYPES.get(_int(pw.get("PowerType")), (f"power type {pw.get('PowerType')}", 1))
                cost, unit = _int(pw["ManaCost"]) / scale, name
        cast = self.cast.get(misc.get("CastingTimeIndex", ""))
        cd = self.cooldowns.get(sid, {})
        recovery = max(_int(cd.get("RecoveryTime")), _int(cd.get("CategoryRecoveryTime")))
        duration = self.durations.get(misc.get("DurationIndex", ""))
        rng = self.ranges.get(misc.get("RangeIndex", ""))
        return SpellBase(spell.name, sid, lv, cost, unit, float(cast) if cast is not None else None,
                         float(recovery) if recovery else None, float(_int(cd.get("StartRecoveryTime"))) if cd.get("StartRecoveryTime") else None,
                         float(duration) if duration and duration > 0 else None, rng if rng else None)


def _scaled(prop: str, unit: str, value: float) -> float:
    """A modifier's flat value in the property's display units (rage modifiers are in tenths)."""
    if prop == "cost" and unit in ("rage", "runic power"):
        return value / 10
    return value


def describe_amount(prop: str, unit: str, value: float, flat: bool) -> str:
    if not flat or prop in ("critical strike chance", "hit chance"):
        return f"{value:+g}%"
    if prop in ("cast time", "cooldown", "global cooldown", "duration"):
        return f"{value / 1000:+g} s"
    if prop == "range":
        return f"{value:+g} yd"
    return f"{_scaled(prop, unit, value):+g} {unit}"


def spell_changes(mods: list[SpellModifier], catalogue: Catalogue, book: SpellBook, class_name: str, level: int,
                  ranks: dict[str, int]) -> list[SpellChange]:
    """What the talents (talent spell id -> ranks) do to the class's spells: (base + flat) x (1 + sum of
    percents), per spell and property. Only modifiers that agree with their text are applied. The
    order and how percents combine are not checked in game (the plan's open questions)."""
    parts: dict[tuple[str, str], list[tuple[str, float, bool]]] = defaultdict(list)
    for m in mods:
        r = ranks.get(m.spell_id, 0)
        if m.class_name != class_name or not r or not m.clean or m.op not in PROPERTY_OPS or r not in m.by_rank:
            continue
        for spell in m.matched:
            parts[(spell, PROPERTY_OPS[m.op])].append((m.talent, m.by_rank[r], m.flat))
    spells = catalogue.by_class.get(class_name, {})
    out = []
    for (name, prop), items in sorted(parts.items()):
        base = book.base(spells[name], level)
        attr = prop.replace(" ", "_")
        start = getattr(base, attr)
        if start is None:
            continue
        unit = base.cost_unit if prop == "cost" else ""
        flat = sum(_scaled(prop, unit, v) for _, v, is_flat in items if is_flat)
        pct = sum(v for _, v, is_flat in items if not is_flat)
        value = max(0.0, (start + flat) * (1 + pct / 100))
        if value == start:
            continue
        out.append(SpellChange(name, prop, start, value, unit,
                               tuple((t, describe_amount(prop, unit, v, f)) for t, v, f in items)))
    return out


def format_value(prop: str, unit: str, value: float) -> str:
    if prop in ("cast time", "cooldown", "global cooldown", "duration"):
        seconds = value / 1000
        if seconds >= 120:
            return f"{seconds / 60:g} min"
        return f"{round(seconds, 2):g} s"
    if prop == "range":
        return f"{round(value, 1):g} yd"
    return f"{round(value, 1):g}{'%' if unit.startswith('%') else ''} {unit.removeprefix('% ')}".strip()


# ---- phase 3: bonuses the talents give each spell (no base value yet: damage numbers are phase 4)

# Op -> (bonus, applied as a flat amount?). Every talent using these ops stores them in this one form
# (crit and hit chance as percentage points, the rest as percents); the other form is not applied.
BONUS_OPS: dict[int, tuple[str, bool]] = {
    0: ("damage or healing", False),
    22: ("periodic damage or healing", False),
    7: ("critical strike chance", True),
    15: ("critical strike bonus", False),
    16: ("hit chance", True),
    2: ("threat", False),
    9: ("chance not to lose casting time", False),
}


def applies(m: SpellModifier) -> bool:
    """Is this modifier applied by phases 2-3? It must agree with its text and have values."""
    if not (m.clean and m.by_rank):
        return False
    if m.op in PROPERTY_OPS:
        return True
    return m.op in BONUS_OPS and m.flat == BONUS_OPS[m.op][1]


@dataclass(frozen=True)
class SpellBonus:
    spell: str
    bonus: str
    total: float                       # percent (or percentage points for chances)
    by: tuple[tuple[str, str], ...]    # (talent, "+6%")


def spell_bonuses(mods: list[SpellModifier], class_name: str, ranks: dict[str, int]) -> list[SpellBonus]:
    """Per spell and bonus, the talents' amounts added together (how the game combines them is not
    checked in game)."""
    parts: dict[tuple[str, str], list[tuple[str, float]]] = defaultdict(list)
    for m in mods:
        r = ranks.get(m.spell_id, 0)
        if m.class_name != class_name or not r or m.op not in BONUS_OPS or not applies(m) or r not in m.by_rank:
            continue
        for spell in m.matched:
            parts[(spell, BONUS_OPS[m.op][0])].append((m.talent, m.by_rank[r]))
    return [SpellBonus(spell, bonus, sum(v for _, v in items), tuple((t, f"{v:+g}%") for t, v in items))
            for (spell, bonus), items in sorted(parts.items()) if sum(v for _, v in items)]


AUDIT_HEADER = ("class", "talent", "effect", "op", "flat_or_percent", "top_rank_value", "spells_reached", "text_names", "problems")


def audit_rows(mods: list[SpellModifier]) -> list[tuple]:
    return [(m.class_name, m.talent, m.effect_index, m.op_name, "flat" if m.flat else "percent",
             f"{m.by_rank[max(m.by_rank)]:g}" if m.by_rank else "", ", ".join(m.matched),
             ", ".join(m.named + tuple(f"{s} spells" for s in m.schools_named)), "; ".join(m.problems)) for m in mods]


# Audit problem -> a short heading, matched on a phrase in the problem text.
PROBLEM_KINDS = (
    ("valued with the spells' effects", "changes the spell's own effect amounts (phase 4)"),
    ("not established", "what the op changes is not established yet"),
    ("does not say so", "the text does not name what the op changes"),
    ("casts itself", "reaches only pet, poison, totem or weapon spells (later)"),
    ("matches no spell", "the mask matches no spell"),
    ("no spell family", "the talent has no spell family"),
    ("no value per rank", "no value per rank in the data"),
    ("no modifier of the talent", "the text names a spell no modifier reaches"),
    ("spells, but the mask", "reaches spells outside the school the text names"),
    ("which the text does not name", "reaches spells the text does not name"),
)


def problem_kind(problem: str) -> str:
    return next((label for phrase, label in PROBLEM_KINDS if phrase in problem), problem)


def summary(mods: list[SpellModifier]) -> list[str]:
    clean = sum(m.clean for m in mods)
    lines = [f"{len(mods)} spell modifiers on {len({(m.class_name, m.talent) for m in mods})} talents: {clean} agree with their text, "
             f"{len(mods) - clean} need a look"]
    kinds = defaultdict(int)
    for m in mods:
        for p in m.problems:
            kinds[problem_kind(p)] += 1
    for k, n in sorted(kinds.items(), key=lambda x: -x[1]):
        lines.append(f"  {n:4}  {k}")
    return lines


def main(argv: list[str] | None = None) -> int:
    from .talentinfo import _load
    repo_root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description="The spell model: catalogue and talent spell modifiers.")
    ap.add_argument("command", choices=("audit",))
    ap.add_argument("--class", dest="class_name")
    ap.add_argument("--out", type=Path, help="write the audit as a tab-separated file")
    ap.add_argument("--build")
    ap.add_argument("--data-dir", type=Path, default=repo_root / "data")
    args = ap.parse_args(argv)
    loaded = _load(args)
    if loaded is None:
        return 2
    ds, model, build = loaded
    mods = resolve(ds, model, EffectBook.for_model(ds, model))
    if args.class_name:
        mods = [m for m in mods if m.class_name.lower() == args.class_name.lower()]
    print(f"Build {build}")
    print("\n".join(summary(mods)))
    if args.out:
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter="\t")
        writer.writerow(AUDIT_HEADER)
        writer.writerows(audit_rows(mods))
        args.out.write_text(buf.getvalue(), encoding="utf-8", newline="")
        print(f"Written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
