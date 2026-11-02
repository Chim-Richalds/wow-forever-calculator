"""Tests for wowcalc.spells (the spell model, phase 1). Run: python -m unittest discover -s tests"""

import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from wowcalc.dataset import Dataset  # noqa: E402
from wowcalc.effects import EffectBook  # noqa: E402
from wowcalc.fetch import TABLES  # noqa: E402
from wowcalc.spells import (  # noqa: E402
    Catalogue, ClassSpell, SpellBook, SpellModifier, applies, describe_amount, effect_text, format_value, problem_kind, resolve,
    spell_bonuses, spell_changes,
)
from wowcalc.trees import load_model  # noqa: E402


class EffectTextTests(unittest.TestCase):
    def test_the_part_of_the_text_for_each_value(self):
        text = "Reduces the casting time of your Frostbolt spell by $/1000;S1 sec and its Mana cost by $s2%."
        # from the previous value to the next one: the words before and after its own value
        self.assertEqual(effect_text(text, 0), "Reduces the casting time of your Frostbolt spell by $/1000;S1 sec and its Mana cost by ")
        self.assertEqual(effect_text(text, 1), " sec and its Mana cost by $s2%.")
        self.assertEqual(effect_text("Reduces the global cooldown of Slam by ${$m1/-1000}.2 sec.", 0), "Reduces the global cooldown of Slam by ${$m1/-1000}.2 sec.")
        self.assertEqual(effect_text(text, 2), "")      # the text does not show effect 3


class UnitTests(unittest.TestCase):
    def test_amounts_and_values_in_display_units(self):
        self.assertEqual(describe_amount("cast time", "", -500, True), "-0.5 s")
        self.assertEqual(describe_amount("cost", "rage", -30, True), "-3 rage")      # rage modifiers are in tenths
        self.assertEqual(describe_amount("cost", "mana", -15, False), "-15%")
        self.assertEqual(describe_amount("range", "", 6, True), "+6 yd")
        self.assertEqual(format_value("cooldown", "", 180000), "3 min")
        self.assertEqual(format_value("cast time", "", 2500), "2.5 s")
        self.assertEqual(format_value("cost", "% of base mana", 10.2), "10.2% of base mana")


class AppliesTests(unittest.TestCase):
    def mod(self, op, flat, problems=(), by_rank=None):
        return SpellModifier("Mage", "T", "1", 0, op, flat, {1: 2.0} if by_rank is None else by_rank, ("Fireball",), (), (), problems)

    def test_only_clean_modifiers_with_values_in_the_expected_form(self):
        self.assertTrue(applies(self.mod(7, True)))          # crit chance: percentage points
        self.assertFalse(applies(self.mod(7, False)))        # crit chance as a percent: not the form the talents use
        self.assertTrue(applies(self.mod(0, False)))         # damage: percent
        self.assertFalse(applies(self.mod(0, True)))         # flat damage: not applied
        self.assertTrue(applies(self.mod(10, True)))         # cast time: either form is a property change
        self.assertFalse(applies(self.mod(10, True, problems=("x",))))
        self.assertFalse(applies(self.mod(10, True, by_rank={})))
        self.assertFalse(applies(self.mod(3, False)))        # effect amounts: phase 4


class ReachTests(unittest.TestCase):
    def test_only_spells_of_the_same_family_with_a_shared_mask_bit(self):
        cat = Catalogue(by_class={"Mage": {
            "Frostbolt": ClassSpell("Frostbolt", ("116",), "3", ((32, 0, 0, 0),), ("Frost",), "trainer"),
            "Fireball": ClassSpell("Fireball", ("133",), "3", ((1, 0, 0, 0),), ("Fire",), "trainer"),
            "Odd": ClassSpell("Odd", ("9",), "4", ((32, 0, 0, 0),), ("Frost",), "trainer"),
        }})
        self.assertEqual(cat.reached("Mage", "3", (32, 0, 0, 0)), ("Frostbolt",))
        self.assertEqual(cat.reached("Mage", "3", (33, 0, 0, 0)), ("Fireball", "Frostbolt"))
        self.assertEqual(cat.reached("Mage", "3", (0, 0, 0, 0)), ())

    def test_problem_kinds(self):
        self.assertEqual(problem_kind("op 8: changes the spell's own effect amounts; valued with the spells' effects (phase 4)"),
                         "changes the spell's own effect amounts (phase 4)")
        self.assertEqual(problem_kind("something new"), "something new")


PINNED = "1.60.1.70450"
REAL = Path(os.environ.get("WOWCALC_DATA", REPO / "data")) / "raw" / PINNED
COMPLETE = REAL.is_dir() and all((REAL / f"{t}.csv").is_file() for t in TABLES)


@unittest.skipUnless(COMPLETE, f"a complete {PINNED} is not at {REAL}")
class RealDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ds = Dataset(REAL, PINNED)
        model = load_model(ds)
        book = EffectBook.for_model(ds, model)
        cls.catalogue = Catalogue.build(ds, model, book)
        cls.mods = resolve(ds, model, book, cls.catalogue)
        cls.book = SpellBook(ds)

    def mod(self, talent, index=0):
        return next(m for m in self.mods if m.talent == talent and m.effect_index == index)

    def test_a_single_spell_talent_reaches_that_spell(self):
        for talent, spell in (("Improved Cone of Cold", "Cone of Cold"), ("Improved Frost Nova", "Frost Nova"), ("Improved Slam", "Slam")):
            m = self.mod(talent)
            self.assertEqual((m.matched, m.problems), ((spell,), ()), talent)

    def test_a_school_talent_reaches_that_school_only(self):
        m = self.mod("Critical Mass")
        self.assertEqual((m.op_name, m.problems), ("critical strike chance", ()))
        self.assertIn("Fireball", m.matched)
        self.assertNotIn("Frostbolt", m.matched)

    def test_pet_talents_are_set_aside_for_later(self):
        m = self.mod("Improved Imp")
        self.assertEqual(m.matched, ())
        self.assertTrue(any("casts itself" in p for p in m.problems), m.problems)

    def test_text_that_disagrees_with_the_op_is_flagged(self):
        # Arcane Instability's second value is "critical strike chance" in the text, but op 22 in the data
        m = self.mod("Arcane Instability", 1)
        self.assertEqual(m.op, 22)
        self.assertTrue(any("next to the value says critical strike chance" in p for p in m.problems), m.problems)

    def changes(self, class_name, **talents):
        ids = {m.talent: m.spell_id for m in self.mods}
        return {(c.spell, c.prop): (c.base, c.value) for c in spell_changes(self.mods, self.catalogue, self.book, class_name, 60,
                                                                              {ids[t.replace("_", " ")]: r for t, r in talents.items()})}

    def test_cast_time_cooldown_and_cost_changes(self):
        mage = self.changes("Mage", Improved_Frostbolt=5, Improved_Frost_Nova=2)
        self.assertEqual(mage[("Frostbolt", "cast time")], (3000.0, 2500.0))
        self.assertEqual(mage[("Frost Nova", "cooldown")], (25000.0, 21000.0))
        warrior = self.changes("Warrior", Improved_Heroic_Strike=3)
        self.assertEqual(warrior[("Heroic Strike", "cost")], (15.0, 12.0))      # rage, stored in tenths

    def test_percent_cost_changes_apply_to_every_spell_reached(self):
        mage = self.changes("Mage", Frost_Channeling=3)
        self.assertEqual(mage[("Frostbolt", "cost")][1], mage[("Frostbolt", "cost")][0] * 0.85)
        self.assertNotIn(("Fireball", "cost"), mage)

    def test_bonuses_add_up_per_spell(self):
        crit = self.mod("Critical Mass")
        ids = {m.talent: m.spell_id for m in self.mods}
        bonuses = {(b.spell, b.bonus): b.total for b in spell_bonuses(self.mods, "Mage", {ids["Critical Mass"]: 1, ids["Ice Shards"]: 1})}
        self.assertEqual(bonuses[("Fireball", "critical strike chance")], crit.by_rank[1])
        self.assertIn(("Frostbolt", "critical strike bonus"), bonuses)
        self.assertNotIn(("Frostbolt", "critical strike chance"), bonuses)

    def test_a_school_word_inside_a_spell_name_is_not_a_school(self):
        for talent in ("Rapid Killing", "Improved Inner Fire"):     # "Rapid Fire", "Inner Fire"
            for m in self.mods:
                if m.talent == talent:
                    self.assertNotIn("Fire", m.schools_named, talent)

    def test_spell_set_differences_are_notes_not_problems(self):
        convection = self.mod("Convection")       # "your Shock, Lightning Bolt ... spells": Shock names a family
        self.assertEqual(convection.problems, ())
        self.assertFalse(any("Earth Shock" in n for n in convection.notes))
        corruption = self.mod("Improved Corruption")
        self.assertTrue(corruption.clean)

    def test_rune_abilities_from_another_version_are_left_out(self):
        druid = self.catalogue.by_class["Druid"]
        for name in ("Starsurge", "Lifebloom", "Sunfire", "Nourish"):     # only listed as Season of Discovery runes
            self.assertNotIn(name, druid)
        for name in ("Starfire", "Wrath", "Moonfire"):
            self.assertIn(name, druid)
        self.assertIn("Shadow Word: Death", self.catalogue.by_class["Priest"])   # named by the talent Early Demise

    def test_the_spellbook_reference_decides_the_class_spells(self):
        hunter, rogue, priest = (self.catalogue.by_class[c] for c in ("Hunter", "Rogue", "Priest"))
        self.assertIn("Aimed Shot", hunter)               # missed by the class lookup, found by name
        self.assertNotIn("Black Arrow", hunter)           # not in the Forever spellbook, not a talent
        self.assertNotIn("Deadly Poison II", rogue)       # old ranks of poisons
        self.assertIn("Binding Heal", priest)             # still a talent in this build (the 31 October hotfix replaced it)
        self.assertNotIn("Frost Blast", self.catalogue.by_class["Mage"])   # removed in 70707
        self.assertIn("Pyroblast", self.catalogue.by_class["Mage"])        # a talent ability

    def test_every_modifier_is_resolved(self):
        self.assertGreater(len(self.mods), 250)
        self.assertGreater(sum(m.clean for m in self.mods), len(self.mods) // 2)


if __name__ == "__main__":
    unittest.main()
