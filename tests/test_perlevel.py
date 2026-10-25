"""Offline tests for wowcalc.perlevel and its use in rates.py / stats.py. Run: python -m unittest discover -s tests"""

import json
import sys
import unittest
from decimal import Decimal
from fractions import Fraction as F
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from basestats_fixtures import row, tsv  # noqa: E402
from fixtures import TempCase  # noqa: E402
from wowcalc.basestats import parse_base_stats  # noqa: E402
from wowcalc.derived import ArmorRule, PoolRule  # noqa: E402
from wowcalc.perlevel import (  # noqa: E402
    ClientModel, ExpectedError, LevelRate, fit_client, load_expected, mana_total, parse_rate,
)
from wowcalc.rates import AttackPowerRule, MELEE_CRIT, DODGE, ATTACK_POWER, ARMOR, SPELL_CRIT, RateTable, Interval  # noqa: E402
from wowcalc.sheetbase import HEADER, Figure, SheetBase  # noqa: E402
from wowcalc.stats import build_sheet  # noqa: E402

HEAD = "ID,Level,ClassID,ContentSetID,BaseMana,HPPerStamina,Field_x,CritPerAgility,SpellCritPerIntellect\n"


class ParseRateTests(unittest.TestCase):
    def test_float_noise_is_removed_and_the_unit_is_percent(self):
        self.assertEqual(parse_rate("0.00249999994"), F(1, 4))     # 1 crit % per 4 Agility
        self.assertEqual(parse_rate("0.00050000002"), F(1, 20))    # 1 crit % per 20 Agility
        self.assertEqual(parse_rate("0.001667"), F(1667, 10000))   # the client itself stores only four digits here

    def test_six_significant_digits_are_kept(self):
        self.assertEqual(parse_rate("0.0012345678"), F(123457, 1000000))

    def test_zero_means_no_such_rate(self):
        self.assertEqual(parse_rate("0"), 0)

    def test_nonsense_is_an_error(self):
        for bad in ("", "abc", "-0.1"):
            with self.assertRaises(ExpectedError):
                parse_rate(bad)


class ManaTests(unittest.TestCase):
    def test_the_first_twenty_intellect_give_one_mana_each_and_the_rest_fifteen(self):
        self.assertEqual(mana_total(1000, 15), 1015)
        self.assertEqual(mana_total(1000, 20), 1020)
        self.assertEqual(mana_total(1000, 21), 1035)
        self.assertEqual(mana_total(1213, 100), 1213 + 20 + 15 * 80)

    def test_the_race_factor_applies_to_the_whole_and_rounds_down(self):
        self.assertEqual(mana_total(1000, 20, F(21, 20)), 1071)    # 1020 x 1.05 = 1071
        self.assertEqual(mana_total(1001, 20, F(21, 20)), 1072)    # 1021 x 1.05 = 1072.05
        self.assertEqual(mana_total(1002, 20, F(21, 20)), 1073)    # 1022 x 1.05 = 1073.1


class LoadTests(TempCase):
    def file(self, body, head=HEAD):
        path = self.root / "PlayerExpectedStat.csv"
        path.write_text(head + body, encoding="utf-8")
        return path

    def test_rates_are_grouped_by_class_and_level(self):
        data = load_expected(self.file("1,1,8,0,55,10,1,0.0606,0.0778\n2,60,8,0,1213,10,287,0.0005,0.0016\n"))
        self.assertEqual(sorted(data), ["Mage"])
        self.assertEqual(data["Mage"][60], LevelRate(F(1, 20), F(16, 100), 1213, 10))

    def test_a_class_without_spell_crit_has_none(self):
        data = load_expected(self.file("1,60,1,0,0,10,287,0.0005,0\n"))
        self.assertIsNone(data["Warrior"][60].intellect_pct)

    def test_unmodelled_classes_and_other_content_sets_are_ignored(self):
        data = load_expected(self.file("1,60,6,0,0,10,1,0.0005,0\n2,60,1,5,0,10,1,0.9,0\n3,60,1,0,0,10,1,0.0005,0\n"))
        self.assertEqual(list(data), ["Warrior"])
        self.assertEqual(data["Warrior"][60].agility_pct, F(1, 20))

    def test_a_missing_column_duplicates_and_empty_files_are_refused(self):
        with self.assertRaisesRegex(ExpectedError, "missing columns"):
            load_expected(self.file("1,60,1,0\n", head="ID,Level,ClassID,ContentSetID\n"))
        with self.assertRaisesRegex(ExpectedError, "appears twice"):
            load_expected(self.file("1,60,1,0,0,10,1,0.0005,0\n2,60,1,0,0,10,1,0.0005,0\n"))
        with self.assertRaisesRegex(ExpectedError, "no usable rows"):
            load_expected(self.file(""))
        with self.assertRaisesRegex(ExpectedError, "cannot read"):
            load_expected(self.root / "absent.csv")

    def test_a_bad_value_names_its_line(self):
        with self.assertRaisesRegex(ExpectedError, "line 2"):
            load_expected(self.file("1,sixty,1,0,0,10,1,0.0005,0\n"))


# ---- a small invented world: a Mage with a crit constant 3, dodge constant 1, spell constant 2 ----------------
RATES = {"Mage": {1: LevelRate(F(1, 5), F(1, 4), 55, 10), 30: LevelRate(F(1, 10), F(1, 8), 700, 10),
                  60: LevelRate(F(1, 20), F(1, 16), 1213, 10)}}
RACES = [("Human", 20, 22, 21), ("Gnome", 21, 25, 19), ("Elf", 25, 23, 20), ("Orc", 17, 19, 23)]
MAGE_RULE = AttackPowerRule(2, 0, -10)


def mage_rows(level, scale=1, mana_slip=0, ap_slip=0):
    rate = RATES["Mage"][level]
    out = []
    for race, agi, intel, strength in RACES:
        a, i, s = agi * scale, intel * scale, strength * scale
        mc = F(3) + a * rate.agility_pct
        out.append({"class": "Mage", "race": race, "strength": s, "agility": a, "intellect": i, "stamina": 20, "health": 100,
                    "mana": mana_total(rate.base_mana, i, F(21, 20) if race == "Gnome" else F(1)) + (mana_slip if race == "Orc" else 0),
                    "mc": mc, "dodge": F(1) + (F(1) if race == "Elf" else 0) + a * rate.agility_pct,
                    "sc": F(2) + a * rate.agility_pct + i * rate.intellect_pct,
                    "ap": 2 * s - 10 + (level - 1) * 0 + (ap_slip if race == "Orc" else 0) + (level - 1) * 3 // 59 * 0})
    return out


def fit(rows_by_level, expected=RATES, ap_rules=None, problems=None):
    problems = [] if problems is None else problems
    model = fit_client(expected, rows_by_level, {"Elf": F(1)}, {"Mage": MAGE_RULE} if ap_rules is None else ap_rules,
                       problems, {"Gnome": F(21, 20)})
    return model, problems


class FitClientTests(unittest.TestCase):
    def test_constants_are_recovered_and_agree_across_levels(self):
        model, problems = fit({1: mage_rows(1), 60: mage_rows(60, scale=2)})
        self.assertEqual(problems, [])
        for table, truth in ((model.crit_const, 3), (model.dodge_const, 1), (model.spell_const, 2)):
            lo, hi = table["Mage"]
            self.assertTrue(lo <= truth <= hi, (lo, hi, truth))
            self.assertLessEqual(hi - lo, F(1, 100))

    def test_a_rate_that_disagrees_with_the_panel_is_reported(self):
        wrong = {"Mage": {1: LevelRate(F(1, 5), F(1, 4), 55, 10), 60: LevelRate(F(1, 10), F(1, 16), 1213, 10)}}   # 60: wrong agility rate
        _, problems = fit({1: mage_rows(1), 60: mage_rows(60)}, expected=wrong)
        self.assertTrue(any("crit rate does not fit" in p and "level 60" in p for p in problems), problems)

    def test_a_mana_typo_names_the_row(self):
        _, problems = fit({1: mage_rows(1, mana_slip=80), 60: mage_rows(60)})
        slip = [p for p in problems if "mana" in p]
        self.assertEqual(len(slip), 1, problems)
        self.assertIn("Orc Mage (level 1)", slip[0])

    def test_a_class_or_level_missing_from_the_table_is_reported(self):
        _, problems = fit({1: mage_rows(1), 60: mage_rows(60)}, expected={})
        self.assertTrue(any("not in the PlayerExpectedStat table" in p for p in problems), problems)
        _, problems = fit({1: mage_rows(1), 60: mage_rows(60)}, expected={"Mage": {1: RATES["Mage"][1]}})
        self.assertTrue(any("no PlayerExpectedStat row for level 60" in p for p in problems), problems)

    def test_attack_power_constants_are_taken_at_both_levels_and_an_odd_row_is_named(self):
        rows1 = mage_rows(1, ap_slip=6)
        model, problems = fit({1: rows1, 60: mage_rows(60)})
        self.assertEqual(model.ap_constants["Mage"], (-10, -10))
        bad = [p for p in problems if "attack power" in p]
        self.assertEqual(len(bad), 1, problems)
        self.assertIn("Orc Mage (level 1)", bad[0])
        self.assertIn("suggests Strength 26", bad[0]) 


class ClientModelTests(unittest.TestCase):
    def model(self, constants=(-17, 160)):
        return ClientModel(rates=RATES, crit_const={"Mage": (F(3), F(3))}, dodge_const={"Mage": (F(1), F(1))},
                           spell_const={"Mage": (F(2), F(2))}, ap_constants={"Paladin": constants})

    def test_the_attack_power_constant_climbs_in_a_straight_line_when_the_step_is_whole(self):
        m = self.model()
        self.assertEqual([m.ap_constant("Paladin", l) for l in (1, 2, 30, 60)], [-17, -14, 70, 160])

    def test_a_fractional_step_gives_the_two_known_levels_only(self):
        m = self.model((-10, -9))
        self.assertEqual((m.ap_constant("Paladin", 1), m.ap_constant("Paladin", 60), m.ap_constant("Paladin", 30)), (-10, -9, None))

    def test_an_unknown_class_has_no_constant(self):
        self.assertIsNone(self.model().ap_constant("Rogue", 30))

    def test_the_file_form_round_trips(self):
        m = self.model()
        back = ClientModel.from_json(json.loads(json.dumps(m.to_json())))
        self.assertEqual((back.rates, back.crit_const, back.ap_constants, back.anchors), (m.rates, m.crit_const, m.ap_constants, m.anchors))


def rate_table():
    table = RateTable()
    table.agility = {"Mage": Interval(F(1, 21), F(1, 19))}
    table.intellect_spell_crit = Interval(F(1, 70), F(1, 50))
    table.attack_power = {"Mage": MAGE_RULE}
    table.spell_classes = frozenset({"Mage"})
    table.racial_dodge = {"Elf": F(1)}
    table.armor = ArmorRule(2, F(0))
    table.mana = PoolRule(15, {"Mage": 900}, {"Gnome": F(21, 20)})
    table.client = ClientModel(RATES, {"Mage": (F(3), F(3))}, {"Mage": (F(1), F(1))}, {"Mage": (F(2), F(2))}, {"Mage": (-10, -10)})
    return table


class RateTableTests(unittest.TestCase):
    def test_exact_rates_replace_the_fitted_range_at_a_level_the_table_covers(self):
        t = rate_table()
        best, err = t.effects("Mage", 0, 40, 16, level=30)
        self.assertEqual((best.melee_crit, best.dodge), (4, 4))                   # 40 x 1/10
        self.assertEqual(best.spell_crit, 4 + 2)                                   # plus 16 x 1/8
        self.assertEqual((err.melee_crit, err.spell_crit, err.dodge), (0, 0, 0))   # nothing uncertain

    def test_without_a_level_the_old_range_is_used(self):
        t = rate_table()
        best, err = t.effects("Mage", 0, 40, 0)
        self.assertEqual(best.melee_crit, 40 * Interval(F(1, 21), F(1, 19)).mid)
        self.assertGreater(err.melee_crit, 0)

    def test_covers(self):
        t = rate_table()
        self.assertTrue(t.covers("Mage", 60) and t.covers("Mage", 30))
        self.assertFalse(t.covers("Mage", 31) or t.covers("Rogue", 30))
        t.client = None
        self.assertTrue(t.covers("Mage", 60))
        self.assertFalse(t.covers("Mage", 30))

    def test_a_panel_estimate_matches_the_panel_at_a_known_level(self):
        t = rate_table()
        got = t.panel_estimate("Mage", "Elf", 60, 20, 25, 23)
        self.assertEqual(got[MELEE_CRIT].value, Decimal("4.25"))      # 3 + 25/20
        self.assertEqual(got[DODGE].value, Decimal("3.25"))           # 1 + racial 1 + 25/20
        self.assertEqual(got[SPELL_CRIT].value, Decimal("4.69"))      # 2 + 1.25 + 23/16 = 4.6875, shown to two decimals
        self.assertEqual(got[ATTACK_POWER].value, 30)
        self.assertEqual(got[ARMOR].value, 50)

    def test_a_panel_estimate_between_the_levels_uses_that_levels_rates(self):
        got = rate_table().panel_estimate("Mage", "Human", 30, 20, 20, 22)
        self.assertEqual(str(got[MELEE_CRIT].value), "5.00")                  # 3 + 20/10
        self.assertEqual(str(got[SPELL_CRIT].value), "6.75")                  # 2 + 20/10 + 22/8

    def test_figures_that_do_not_change_with_level_are_copied_from_a_reference(self):
        ref = {("defense", "Parry Chance"): Figure(Decimal("5.00"), True)}
        got = rate_table().panel_estimate("Mage", "Human", 30, 20, 20, 22, ref)
        self.assertEqual(got[("defense", "Parry Chance")], ref[("defense", "Parry Chance")])
        self.assertNotIn(("melee", "Hit Chance"), got)

    def test_no_estimate_without_a_client_table_or_for_an_unknown_level_or_class(self):
        t = rate_table()
        self.assertIsNone(t.panel_estimate("Mage", "Human", 61, 1, 1, 1))
        self.assertIsNone(t.panel_estimate("Rogue", "Human", 30, 1, 1, 1))
        t.client = None
        self.assertIsNone(t.panel_estimate("Mage", "Human", 30, 1, 1, 1))

    def test_the_client_model_survives_the_rates_file(self):
        t = rate_table()
        back = RateTable.from_json(json.loads(json.dumps(t.to_json())))
        self.assertEqual(back.client.rates, t.client.rates)
        self.assertEqual(back.effects("Mage", 0, 40, 16, level=30)[0], t.effects("Mage", 0, 40, 16, level=30)[0])


class SheetAtOtherLevelsTests(unittest.TestCase):
    """A Mage / Human, level 1: Str 21 Agi 20 Int 22; level 60: Str 42 Agi 40 Int 44 (so level 30 is estimated halfway)."""

    def build(self, level, adjustments=None, rates="default", mana=80):
        stats = parse_base_stats(tsv(row("Mage", "Human", l1=(60, mana, 21, 20, 20, 22, 20), l60=(1200, 1300, 42, 40, 60, 44, 40))))
        panel = SheetBase([dict(zip(HEADER, ["Mage", "Human", "60", s, l, v, u])) for s, l, v, u in
                           (("defense", "Parry Chance", "5.00", "%"), ("melee", "Hit Chance", "0.00", "%"))])
        return build_sheet(stats, "Mage", "Human", level, [], adjustments, panel, rate_table() if rates == "default" else rates)

    def test_a_level_without_a_panel_gets_figures_from_the_client_rates_and_says_so(self):
        sheet = self.build(30)
        base = {k: v.base for k, v in sheet.figures.items()}
        self.assertEqual(str(base["melee_crit"]), "6.00")                       # 3 + 30 x 1/10: the estimated base Agility at level 30 is 30
        self.assertEqual(str(base["parry"]), "5.00")                            # copied from the exported level
        self.assertEqual(sheet.attack_power.base, 2 * sheet.lines["strength"].base - 10)
        self.assertEqual(sheet.armor.base, 2 * sheet.lines["agility"].base)
        self.assertTrue(any("no character panel for level 30" in n and "ESTIMATED" in n for n in sheet.notes), sheet.notes)

    def test_extra_stats_move_the_figures_at_that_level(self):
        plain, more = self.build(30), self.build(30, {"agility": 20, "strength": 5})
        self.assertEqual(more.figures["melee_crit"].from_adjustments, 2)       # 20 x 1/10
        self.assertEqual(more.figures["melee_crit"].uncertainty, 0)
        self.assertEqual(more.attack_power.from_adjustments, 5 * 2)            # 2 attack power per Strength
        self.assertEqual(more.armor.from_adjustments, 40)
        self.assertEqual(plain.armor.from_adjustments, 0)

    def test_mana_at_that_level_uses_the_client_table(self):
        sheet = self.build(30)
        intel = sheet.lines["intellect"].base
        self.assertEqual(sheet.lines["mana"].base, mana_total(700, intel))
        more = self.build(30, {"intellect": 10})
        self.assertEqual(more.lines["mana"].total, mana_total(700, intel + 10))

    def test_an_estimated_base_is_marked_estimated_and_a_given_one_is_not(self):
        self.assertFalse(self.build(30).lines["mana"].base_exact)
        exact = self.build(1, mana=105)
        self.assertTrue(exact.lines["mana"].base_exact)

    def test_at_an_exported_level_a_base_mana_the_rule_reproduces_gains_mana_from_intellect(self):
        sheet = self.build(1, {"intellect": 10}, mana=mana_total(55, 22))
        self.assertEqual(sheet.lines["mana"].total, mana_total(55, 32))

    def test_a_base_mana_the_rule_does_not_reproduce_is_left_alone_and_reported(self):
        sheet = self.build(1, {"intellect": 10}, mana=80)      # the rule says 55 + 20 + 30 = 105
        self.assertEqual((sheet.lines["mana"].base, sheet.lines["mana"].total), (80, 80))
        self.assertTrue(any("gives 105" in n and "base stats say 80" in n for n in sheet.notes), sheet.notes)

    def test_without_the_client_table_the_old_behaviour_remains(self):
        rates = rate_table()
        rates.client = None
        sheet = self.build(30, rates=rates)
        self.assertIsNone(sheet.figures["melee_crit"].base)
        self.assertTrue(any("no base crit" in n for n in sheet.notes))
        self.assertTrue(any("not applied at level 30" in n for n in sheet.notes), sheet.notes)

    def test_the_exported_levels_still_use_the_panel(self):
        sheet = self.build(60)
        self.assertFalse(any("no character panel" in n for n in sheet.notes))


if __name__ == "__main__":
    unittest.main()
