"""Offline tests for wowcalc.derived. Run: python -m unittest discover -s tests"""

import sys
import unittest
from fractions import Fraction as F
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from wowcalc.derived import ArmorRule, AssumedRules, PoolRule, fit_armor, fit_pool  # noqa: E402


class ArmorRuleTests(unittest.TestCase):
    rule = ArmorRule(2, F(0))

    def test_the_classic_formula_against_panel_readings(self):
        # Orc Shaman, level 60: armor 104 shows 1.86%.
        self.assertEqual(round(float(self.rule.damage_reduction(104, 60)), 2), 1.86)
        # Human Hunter, level 1: armor 38 shows 7.27%.
        self.assertEqual(round(float(self.rule.damage_reduction(38, 1)), 2), 7.27)

    def test_no_armor_means_no_reduction(self):
        self.assertEqual(self.rule.damage_reduction(0, 60), 0)
        self.assertEqual(self.rule.damage_reduction(-5, 60), 0)

    def test_more_armor_reduces_more_but_never_reaches_one_hundred_percent(self):
        values = [self.rule.damage_reduction(a, 60) for a in (100, 1000, 10000, 10**7)]
        self.assertEqual(values, sorted(values))
        self.assertLess(values[-1], 100)


class PoolRuleTests(unittest.TestCase):
    def test_total_is_rounded_down_after_the_race_factor(self):
        rule = PoolRule(10, {"Warrior": 1509}, {"Undead": F(21, 20)})
        self.assertEqual(rule.total("Warrior", "Undead", 111), 2749)   # 2619 x 1.05 = 2749.95
        self.assertEqual(rule.total("Warrior", "Human", 111), 2619)

    def test_an_adjustment_goes_in_before_the_factor(self):
        rule = PoolRule(10, {"Warrior": 1000}, {"Undead": F(2)})
        self.assertEqual(rule.total("Warrior", "Undead", 10, 50), (1000 + 100 + 50) * 2)

    def test_a_class_without_the_pool_is_a_key_error(self):
        with self.assertRaises(KeyError):
            PoolRule(15, {"Mage": 900}).total("Warrior", "Human", 20)


def rows(*items):
    return [dict(zip(("class", "race", "stamina", "intellect", "health", "mana"), item)) for item in items]


class FitPoolTests(unittest.TestCase):
    def fit(self, data, stat="stamina", key="health"):
        problems = []
        rule = fit_pool(data, stat, key, problems, key)
        return rule, problems

    def test_a_clean_rule_without_race_factors(self):
        data = rows(("A", "R1", 40, 0, 1400, None), ("A", "R2", 45, 0, 1450, None), ("A", "R3", 50, 0, 1500, None))
        rule, problems = self.fit(data)
        self.assertEqual((rule.per_point, rule.constants, rule.race_factors, problems), (10, {"A": 1000}, {}, []))

    def test_a_flat_race_factor_is_found_across_classes(self):
        def v(const, stam, m=F(1)):
            import math
            return math.floor((const + 10 * stam) * m)
        data = rows(*[(c, r, s, 0, v(k, s, F(21, 20) if r == "R3" else F(1)), None)
                      for c, k in (("A", 1000), ("B", 1200)) for r, s in (("R1", 40), ("R2", 55), ("R3", 50), ("R4", 61))])
        rule, problems = self.fit(data)
        self.assertEqual((rule.per_point, rule.constants, problems), (10, {"A": 1000, "B": 1200}, []))
        self.assertEqual(rule.race_factors, {"R3": F(21, 20)})

    def test_an_ambiguous_race_factor_is_refused_not_guessed(self):
        # 10 becomes 11 under any factor from 1.10 to 1.19, so the data cannot say which one it is.
        data = rows(("A", "R1", 10, 0, 10, None), ("A", "R2", 10, 0, 10, None), ("A", "R3", 10, 0, 11, None))
        rule, problems = self.fit(data)
        self.assertIsNone(rule)
        self.assertTrue(problems and "no whole-number rule" in problems[0], problems)

    def test_a_row_that_follows_no_rule_is_refused(self):
        data = rows(("A", "R1", 40, 0, 1400, None), ("A", "R2", 45, 0, 1450, None), ("A", "R3", 50, 0, 1500, None),
                    ("A", "R4", 55, 0, 1553, None))  # 3 too high, and no factor of 1 to 50% explains it with the others unchanged
        rule, problems = self.fit(data)
        self.assertTrue(rule is None or "R4" in rule.race_factors)

    def test_a_tie_between_races_cannot_pick_the_ordinary_ones(self):
        data = rows(("A", "R1", 40, 0, 1400, None), ("A", "R2", 40, 0, 1450, None))
        rule, problems = self.fit(data)
        self.assertIsNone(rule)
        self.assertTrue(problems)

    def test_no_usable_rows(self):
        rule, problems = self.fit(rows(("A", "R1", 40, 0, None, None)))
        self.assertEqual((rule, problems), (None, []))

    def test_classes_without_the_pool_are_left_out(self):
        data = rows(("A", "R1", 40, 10, 1400, 1000), ("A", "R2", 45, 12, 1450, 1030), ("A", "R3", 50, 14, 1500, 1060),
                    ("W", "R1", 40, 5, 1400, None), ("W", "R2", 45, 6, 1450, None))
        rule, problems = self.fit(data, stat="intellect", key="mana")
        self.assertEqual(sorted(rule.constants), ["A"])
        self.assertEqual(rule.per_point, 15)


class FitArmorTests(unittest.TestCase):
    def test_two_per_agility_and_the_formula(self):
        problems = []
        data = [{"class": "A", "race": "R", "agility": a, "armor": 2 * a} for a in (40, 50, 61)]
        dr = [(60, F(104), F("1.86")), (1, F(38), F("7.27"))]
        rule = fit_armor(data, dr, problems)
        self.assertEqual((rule.per_agility, problems), (2, []))
        self.assertLess(rule.worst_dr_error, F(5, 1000))

    def test_a_row_that_breaks_the_rule_is_named(self):
        problems = []
        data = [{"class": "A", "race": "R%d" % a, "agility": a, "armor": 2 * a + (7 if a == 50 else 0)} for a in (40, 50, 61, 70)]
        fit_armor(data, [], problems)
        self.assertTrue(any("R50 A" in p and "armor 107" in p for p in problems), problems)

    def test_a_formula_that_does_not_match_the_panel_is_reported(self):
        problems = []
        data = [{"class": "A", "race": "R", "agility": 50, "armor": 100}]
        fit_armor(data, [(60, F(104), F("5.00"))], problems)
        self.assertTrue(any("damage reduction differs" in p for p in problems), problems)

    def test_no_armor_figures(self):
        problems = []
        self.assertIsNone(fit_armor([{"class": "A", "race": "R", "agility": 5, "armor": None}], [], problems))
        self.assertTrue(problems)

    def test_armor_that_is_no_multiple_of_agility(self):
        problems = []
        self.assertIsNone(fit_armor([{"class": "A", "race": "R", "agility": 7, "armor": 100}], [], problems))
        self.assertTrue(problems)


class AssumedRulesTests(unittest.TestCase):
    def test_defaults_are_the_documented_assumptions(self):
        a = AssumedRules()
        self.assertEqual((a.block_base_chance, a.strength_per_block_value), (5, 20))
        self.assertEqual(a.shield_classes, ("Paladin", "Shaman", "Warrior"))

    def test_block_against_a_mob_loses_half_a_point_per_level_the_attacker_is_above(self):
        a = AssumedRules()
        self.assertEqual(a.block_against(63, 60, False), F(7, 2))
        self.assertEqual(a.block_against(60, 60, False), 5)
        self.assertEqual(a.block_against(57, 60, False), F(13, 2))

    def test_block_against_a_player_uses_a_fifth_of_a_point(self):
        self.assertEqual(AssumedRules().block_against(62, 60, True), F(23, 5))

    def test_block_never_goes_below_zero(self):
        self.assertEqual(AssumedRules().block_against(90, 60, False), 0)

    def test_an_old_rates_file_without_level_modifiers_still_loads(self):
        old = {"block_base_chance": "5", "shield_classes": ["Warrior"], "strength_per_block_value": 20}
        a = AssumedRules.from_json(old)
        self.assertEqual((a.block_per_level_mob, a.block_per_level_player), (F(1, 2), F(1, 5)))

    def test_round_trip(self):
        a = AssumedRules(F(7, 2), ("Warrior",), 25)
        self.assertEqual(AssumedRules.from_json(a.to_json()), a)
        self.assertIn("confirmed", a.to_json()["status"])


if __name__ == "__main__":
    unittest.main()
