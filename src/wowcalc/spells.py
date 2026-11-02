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
        names = {r["ID"]: r["Name_lang"] for r in ds.rows("SpellName")}
        options = {}
        for r in ds.rows("SpellClassOptions"):
            options[r["SpellID"]] = (r["SpellClassSet"], tuple(_u32(r[f"SpellClassMask_{i}"]) for i in range(4)))
        school_names = book.schools
        school_mask = {r["SpellID"]: _int(r.get("SchoolMask")) for r in ds.rows("SpellMisc")}
        learnable: dict[str, dict[str, str]] = defaultdict(dict)   # class -> spell id -> source
        for r in ds.rows("SkillLineAbility"):
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
    spell_names = {r["ID"]: r["Name_lang"] for r in ds.rows("SpellName")}
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
