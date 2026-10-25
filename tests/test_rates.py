"""Offline tests for wowcalc.rates. Run: python -m unittest discover -s tests"""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from fractions import Fraction as F
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from basestats_fixtures import row, tsv  # noqa: E402
from fixtures import TempCase  # noqa: E402
from wowcalc import rates  # noqa: E402
from wowcalc.basestats import parse_base_stats  # noqa: E402
from wowcalc.rates import (  # noqa: E402
    AttackPowerRule, Interval, RateTable, RatesError, apply_references, fit_rates, load_rates, slope_interval,
)
from wowcalc.sheetbase import HEADER, SheetBase  # noqa: E402

TOL = F(5, 1000)
LEVEL_ONE = (50, 100, 5, 5, 5, 5, 5)


class SlopeIntervalTests(unittest.TestCase):
    def test_a_known_slope_is_inside_its_interval(self):
        pts = [(F(x), F(x, 20) + F(3)) for x in (50, 55, 60, 65)]
        iv = slope_interval(pts, TOL)
        self.assertIn(F(1, 20), iv)
        self.assertLess(iv.hi - iv.lo, F(1, 100))

    def test_rounding_to_two_decimals_never_excludes_the_true_slope(self):
        for agi in ([40, 42, 47, 55], [120, 127, 130]):
            for per in (20, 29, 53):
                pts = [(F(a), F(round(100 * (F(a, per) + F(7, 3)))) / 100) for a in agi]
                self.assertIn(F(1, per), slope_interval(pts, TOL), (agi, per))

    def test_more_spread_gives_a_tighter_interval(self):
        near = slope_interval([(F(60), F("3.0")), (F(61), F("3.05"))], TOL)
        far = slope_interval([(F(60), F("3.0")), (F(80), F("4.0"))], TOL)
        self.assertLess(far.hi - far.lo, near.hi - near.lo)

    def test_inconsistent_points_have_no_slope(self):
        self.assertIsNone(slope_interval([(F(60), F(1)), (F(70), F(2)), (F(80), F(2))], TOL))

    def test_equal_inputs_measure_nothing(self):
        self.assertIsNone(slope_interval([(F(60), F(1)), (F(60), F(1))], TOL))
        self.assertIsNone(slope_interval([(F(60), F(1))], TOL))

    def test_the_midpoint_and_half_width(self):
        iv = Interval(F(1, 10), F(3, 10))
        self.assertEqual((iv.mid, iv.half_width), (F(1, 5), F(1, 10)))
        self.assertEqual(iv.intersect(Interval(F(2, 10), F(5, 10))), Interval(F(2, 10), F(3, 10)))
        self.assertIsNone(iv.intersect(Interval(F(4, 10), F(5, 10))))


# A small invented world: one caster class and one melee class, each with several races.
# Rules: crit = 2 + agility/20; spell crit = crit + 1 + intellect/60; dodge = crit + 1 (+1 more for "Elf");
# attack power = 2 x strength - 10 (casters) or strength + agility + 100 (melee).
CASTER = [("Human", 35, 125, 20), ("Orc", 37, 121, 25), ("Gnome", 38, 133, 18), ("Elf", 45, 120, 17), ("Troll", 41, 116, 22)]
MELEE = [("Human", 121, 64, 50), ("Orc", 122, 62, 55), ("Elf", 130, 65, 48), ("Troll", 127, 61, 52)]


def r2(x):
    return F(round(100 * x), 100)


HEALTH_BASE = {"Mage": 1000, "Hunter": 1300}
MANA_BASE = {"Mage": 900, "Hunter": 1400}


def stamina_of(strength):
    return 30 + strength // 2


def health_of(cls, race, strength):
    return (HEALTH_BASE[cls] + 10 * stamina_of(strength)) * (21 if race == "Troll" else 20) // 20  # Troll: +5% health


def mana_of(cls, race, intel):
    return (MANA_BASE[cls] + 15 * intel) * (21 if race == "Human" else 20) // 20  # Human: +5% mana


def world(caster_rate=20, melee_rate=53, intellect_rate=60, bonus_elf=1):
    stat_rows, panel = [], []
    for cls, races, per, is_caster in (("Mage", CASTER, caster_rate, True), ("Hunter", MELEE, melee_rate, True)):
        for race, agi, intel, strength in races:
            stat_rows.append(row(cls, race, l1=LEVEL_ONE, l60=(health_of(cls, race, strength), mana_of(cls, race, intel),
                                                                strength, agi, stamina_of(strength), intel, 60)))
            crit = r2(2 + F(agi, per))
            sc = r2(crit + 1 + F(intel, intellect_rate))
            dodge = r2(crit + 1 + (bonus_elf if race == "Elf" else 0))
            ap = 2 * strength - 10 if cls == "Mage" else strength + agi + 100
            armor = 2 * agi
            dr = r2(100 * F(armor, armor + 85 * 60 + 400))
            figs = [("melee", "Crit Chance", crit, "%"), ("spell", "Spell Crit", sc, "%"),
                    ("defense", "Dodge Chance", dodge, "%"), ("melee", "Attack Power", F(ap), ""),
                    ("defense", "Armor", F(armor), ""), ("defense", "Damage Reduction", dr, "%")]
            for sec, lab, val, unit in figs:
                panel.append(dict(zip(HEADER, [cls, race, "60", sec, lab, format(float(val), "g"), unit])))
    table = parse_base_stats(tsv(*stat_rows))
    assert not table.problems, table.problems
    return table, SheetBase(panel)


class FitTests(unittest.TestCase):
    def setUp(self):
        self.stats, self.panel = world()
        self.table = fit_rates(self.stats, self.panel)

    def test_the_invented_world_fits_cleanly(self):
        self.assertEqual(self.table.problems, [])

    def test_the_true_rates_are_inside_the_fitted_ranges(self):
        self.assertIn(F(1, 20), self.table.agility["Mage"])
        self.assertIn(F(1, 53), self.table.agility["Hunter"])
        self.assertIn(F(1, 60), self.table.intellect_spell_crit)

    def test_attack_power_rules_are_recovered_exactly(self):
        self.assertEqual(self.table.attack_power["Mage"], AttackPowerRule(2, 0, -10, False))
        self.assertEqual(self.table.attack_power["Hunter"], AttackPowerRule(1, 1, 100, False))

    def test_armor_per_agility_and_the_damage_reduction_formula_are_recovered(self):
        self.assertEqual(self.table.armor.per_agility, 2)
        self.assertLessEqual(self.table.armor.worst_dr_error, F(5, 1000))

    def test_a_different_damage_reduction_formula_is_reported(self):
        rows = [dict(r, value=format(float(F(r["value"]) * F(11, 10)), "g")) if r["label"] == "Damage Reduction" else r for r in self._rows()]
        table = fit_rates(self.stats, SheetBase(rows))
        self.assertTrue(any("damage reduction differs" in p for p in table.problems), table.problems)

    def test_armor_that_is_not_a_multiple_of_agility_is_reported(self):
        rows = [dict(r, value="999") if (r["race"], r["label"]) == ("Orc", "Armor") and r["class"] == "Mage" else r for r in self._rows()]
        table = fit_rates(self.stats, SheetBase(rows))
        self.assertTrue(any("Orc Mage" in p and "armor" in p for p in table.problems), table.problems)

    def test_health_and_mana_rules_with_their_race_factors_are_recovered(self):
        self.assertEqual(self.table.health.per_point, 10)
        self.assertEqual(self.table.mana.per_point, 15)
        self.assertEqual(self.table.health.constants, HEALTH_BASE)
        self.assertEqual(self.table.mana.constants, MANA_BASE)
        self.assertEqual(self.table.health.race_factors, {"Troll": F(21, 20)})
        self.assertEqual(self.table.mana.race_factors, {"Human": F(21, 20)})

    def test_the_pool_rule_reproduces_every_row_exactly(self):
        for cls, races in (("Mage", CASTER), ("Hunter", MELEE)):
            for race, agi, intel, strength in races:
                self.assertEqual(self.table.health.total(cls, race, stamina_of(strength)), health_of(cls, race, strength))
                self.assertEqual(self.table.mana.total(cls, race, intel), mana_of(cls, race, intel))

    def test_a_pool_that_follows_no_rule_is_reported(self):
        rows = [dict(r) for r in self._rows()]
        stats = parse_base_stats(tsv(*[row(c, r, l1=LEVEL_ONE, l60=(health_of(c, r, st) + (37 if (c, r) == ("Mage", "Orc") else 0), mana_of(c, r, i),
                                                                      st, a, stamina_of(st), i, 60))
                                       for c, races in (("Mage", CASTER), ("Hunter", MELEE)) for r, a, i, st in races]))
        table = fit_rates(stats, self.panel)
        self.assertTrue(any(p.startswith("health:") for p in table.problems), table.problems)

    def test_agility_at_another_level_is_audited_through_armor(self):
        rows = self._rows()
        for cls, race, st, a, it in (("Mage", "Human", 20, 20, 23),):
            rows.append(dict(zip(HEADER, [cls, race, "1", "defense", "Armor", "50", ""])))  # Agility-L1 below says 20, so armor should be 40
        stats = parse_base_stats(tsv(*[row(c, r, l1=(50, 100, 5, 20 if (c, r) == ("Mage", "Human") else 5, 5, 5, 5),
                                           l60=(health_of(c, r, st), mana_of(c, r, i), st, a, stamina_of(st), i, 60))
                                       for c, races in (("Mage", CASTER), ("Hunter", MELEE)) for r, a, i, st in races]))
        table = fit_rates(stats, SheetBase(rows))
        self.assertTrue(any("Human Mage (level 1)" in p and "armor suggests 25" in p for p in table.problems), table.problems)

    def test_a_flat_racial_dodge_bonus_is_detected_and_removed(self):
        self.assertEqual(self.table.racial_dodge, {"Elf": F(1)})

    def test_a_typo_in_a_base_stat_is_caught(self):
        stats = parse_base_stats(tsv(*[row("Mage", race, l1=LEVEL_ONE, l60=(health_of("Mage", race, st), mana_of("Mage", race, it), st,
                                                                              agi + (30 if race == "Gnome" else 0), stamina_of(st), it, 60))
                                       for race, agi, it, st in CASTER]))
        table = fit_rates(stats, self.panel)
        self.assertTrue(any("Mage" in p and "agility rate" in p for p in table.problems), table.problems)

    def test_a_non_integer_attack_power_rule_is_reported(self):
        panel = SheetBase([dict(r, value="333" if (r["race"], r["label"]) == ("Orc", "Attack Power") else r["value"])
                           for r in self._rows()])
        table = fit_rates(self.stats, panel)
        self.assertTrue(any("attack power" in p for p in table.problems))

    def _rows(self):
        rows = []
        for key, figs in self.panel._figures.items():
            for (sec, lab), fig in figs.items():
                rows.append(dict(zip(HEADER, [key[0], key[1], str(key[2]), sec, lab, str(fig.value), "%" if fig.percent else ""])))
        return rows

    def test_dodge_that_does_not_follow_the_crit_rate_is_reported(self):
        rows = self._rows()
        for r in rows:  # one race's dodge is off by 0.3, which is no whole-number racial bonus
            if (r["class"], r["race"], r["label"]) == ("Mage", "Orc", "Dodge Chance"):
                r["value"] = format(float(F(r["value"]) + F(3, 10)), "g")
        table = fit_rates(self.stats, SheetBase(rows))
        self.assertTrue(any("dodge does not follow" in p for p in table.problems), table.problems)

    def test_classes_that_do_not_share_an_intellect_rate_are_reported(self):
        stats, panel = world(intellect_rate=60)
        rows = [dict(r) for r in self._rows()]
        for r in rows:  # make the Hunter's spell crit depend on Intellect twice as strongly
            if r["class"] == "Hunter" and r["label"] == "Spell Crit":
                intel = {n: i for n, _, i, _ in MELEE}[r["race"]]
                agi = {n: a for n, a, _, _ in MELEE}[r["race"]]
                r["value"] = format(float(r2(r2(2 + F(agi, 53)) + 1 + F(intel, 20))), "g")
        table = fit_rates(stats, SheetBase(rows))
        self.assertTrue(any("intellect" in p.lower() for p in table.problems), table.problems)


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.table = fit_rates(*world())

    def test_a_reference_inside_the_measured_range_becomes_the_point_estimate(self):
        apply_references(self.table, {"source": "x", "build": "b", "agility_per_crit": {"Mage": "19.8", "Hunter": "53"},
                                      "intellect_per_spell_crit": {"Mage": "59.5"}})
        self.assertEqual(self.table.agility_points["Mage"], 1 / F("19.8"))
        self.assertEqual(self.table.intellect_points["Mage"], 1 / F("59.5"))
        self.assertEqual(self.table.problems, [])
        best, _ = self.table.effects("Mage", 0, 198, 0)
        self.assertEqual(best.melee_crit, 10)  # 198 Agility at 19.8 per 1% is exactly 10%, not the midpoint's answer

    def test_a_reference_outside_the_range_is_reported_and_not_used(self):
        apply_references(self.table, {"agility_per_crit": {"Mage": "14.5"}, "intellect_per_spell_crit": {"Mage": "30"}})
        self.assertNotIn("Mage", self.table.agility_points)
        self.assertNotIn("Mage", self.table.intellect_points)
        self.assertEqual(len([p for p in self.table.problems if "reference" in p]), 2)

    def test_the_uncertainty_does_not_shrink_because_a_reference_agrees(self):
        _, before = self.table.effects("Mage", 0, 100, 0)
        apply_references(self.table, {"agility_per_crit": {"Mage": "19.8"}})
        _, after = self.table.effects("Mage", 0, 100, 0)
        self.assertEqual(before.melee_crit, after.melee_crit)

    def test_unknown_classes_in_a_reference_are_ignored(self):
        apply_references(self.table, {"agility_per_crit": {"Necromancer": "20"}})
        self.assertEqual(self.table.agility_points, {})

    def test_a_reference_attack_power_rate_replaces_the_fit_for_changes_only(self):
        rule = self.table.attack_power["Mage"]
        other = self.table.attack_power["Hunter"]
        apply_references(self.table, {"attack_power_per_strength": {"Mage": rule.per_strength + 3, "Hunter": other.per_strength},
                                      "attack_power_per_agility": {"Mage": rule.per_agility, "Hunter": other.per_agility}})
        best, _ = self.table.effects("Mage", 10, 0, 0)
        self.assertEqual(best.attack_power, 10 * (rule.per_strength + 3))
        self.assertEqual(self.table.attack_power["Mage"], rule)      # the fitted rule is untouched
        self.assertEqual(len(self.table.references["attack_power_overrides"]), 1)  # Hunter agreed, so no override
        self.assertNotIn("Hunter", self.table.attack_power_points)

    def test_a_reference_attack_power_rate_for_agility_applies_too(self):
        rule = self.table.attack_power["Hunter"]
        apply_references(self.table, {"attack_power_per_agility": {"Hunter": rule.per_agility + 2}})
        best, _ = self.table.effects("Hunter", 0, 5, 0)
        self.assertEqual(best.attack_power, 5 * (rule.per_agility + 2))
        best, _ = self.table.effects("Hunter", 4, 0, 0)
        self.assertEqual(best.attack_power, 4 * rule.per_strength)   # Strength kept the fitted rate

    def test_attack_power_overrides_round_trip(self):
        rule = self.table.attack_power["Mage"]
        apply_references(self.table, {"attack_power_per_strength": {"Mage": rule.per_strength + 1}})
        back = RateTable.from_json(json.loads(json.dumps(self.table.to_json())))
        self.assertEqual(back.attack_power_points, self.table.attack_power_points)
        self.assertEqual(back.effects("Mage", 2, 0, 0)[0].attack_power, 2 * (rule.per_strength + 1))

    def test_references_round_trip_through_the_file(self):
        apply_references(self.table, {"source": "s", "build": "b", "agility_per_crit": {"Mage": "19.8"}})
        back = RateTable.from_json(json.loads(json.dumps(self.table.to_json())))
        self.assertEqual((back.agility_points, back.references["source"]), (self.table.agility_points, "s"))
        self.assertEqual((back.health, back.mana, back.armor), (self.table.health, self.table.mana, self.table.armor))


class EffectsTests(unittest.TestCase):
    def setUp(self):
        self.table = fit_rates(*world())

    def test_agility_moves_crit_dodge_and_spell_crit_together(self):
        best, err = self.table.effects("Mage", 0, 20, 0)
        self.assertAlmostEqual(float(best.melee_crit), 1.0, delta=0.05)
        self.assertEqual((best.melee_crit, best.dodge), (best.melee_crit, best.melee_crit))
        self.assertEqual(best.spell_crit, best.melee_crit)
        self.assertGreater(err.melee_crit, 0)

    def test_intellect_moves_only_spell_crit(self):
        best, _ = self.table.effects("Mage", 0, 0, 60)
        self.assertEqual((best.melee_crit, best.dodge), (0, 0))
        self.assertAlmostEqual(float(best.spell_crit), 1.0, delta=0.05)

    def test_strength_and_agility_move_attack_power_by_whole_numbers(self):
        self.assertEqual(self.table.effects("Mage", 10, 7, 0)[0].attack_power, 20)
        self.assertEqual(self.table.effects("Hunter", 10, 7, 0)[0].attack_power, 17)

    def test_intellect_adds_uncertainty_to_spell_crit_only(self):
        _, err = self.table.effects("Mage", 0, 0, 60)
        self.assertGreater(err.spell_crit, 0)
        self.assertEqual((err.melee_crit, err.dodge), (0, 0))

    def test_no_change_means_no_effect_and_no_uncertainty(self):
        best, err = self.table.effects("Mage", 0, 0, 0)
        self.assertEqual((best.melee_crit, best.spell_crit, best.dodge, best.attack_power), (0, 0, 0, 0))
        self.assertEqual((err.melee_crit, err.spell_crit), (0, 0))

    def test_uncertainty_grows_with_the_size_of_the_change(self):
        small = self.table.effects("Mage", 0, 10, 0)[1].melee_crit
        large = self.table.effects("Mage", 0, 100, 0)[1].melee_crit
        self.assertEqual(large, 10 * small)

    def test_a_decrease_has_a_positive_uncertainty(self):
        self.assertGreater(self.table.effects("Mage", 0, -50, 0)[1].melee_crit, 0)

    def test_an_unfitted_class_is_a_key_error(self):
        with self.assertRaises(KeyError):
            self.table.effects("Warrior", 1, 1, 1)

    def test_a_class_without_spell_crit_gets_none(self):
        table = RateTable(agility={"Warrior": Interval(F(1, 20), F(1, 20))}, attack_power={"Warrior": AttackPowerRule(2, 0, 160)},
                          intellect_spell_crit=Interval(F(1, 60), F(1, 60)), spell_classes=frozenset({"Mage"}))
        self.assertIsNone(table.effects("Warrior", 0, 0, 10)[0].spell_crit)


class FileTests(TempCase):
    def test_json_round_trip(self):
        table = fit_rates(*world())
        path = self.root / "r.json"
        path.write_text(json.dumps(table.to_json()), encoding="utf-8")
        back = load_rates(path)
        self.assertEqual(back.agility, table.agility)
        self.assertEqual(back.attack_power, table.attack_power)
        self.assertEqual(back.intellect_spell_crit, table.intellect_spell_crit)
        self.assertEqual(back.spell_classes, table.spell_classes)
        self.assertEqual(back.racial_dodge, table.racial_dodge)

    def test_bad_files_are_refused(self):
        for text in ("not json", "{}", '{"level": 60}', "[]"):
            path = self.root / "r.json"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(RatesError):
                load_rates(path)
        with self.assertRaises(RatesError):
            load_rates(self.root / "missing.json")

    def test_an_empty_range_is_refused(self):
        data = fit_rates(*world()).to_json()
        data["agility"]["Mage"] = ["1/10", "1/20"]
        with self.assertRaises(RatesError):
            RateTable.from_json(data)

    def test_cli_fit_and_show(self):
        ext = self.root / "data" / "external"
        ext.mkdir(parents=True)
        stats, panel = world()
        (ext / "base_stats.tsv").write_text(tsv(*[row(c, r, l1=LEVEL_ONE, l60=(health_of(c, r, st), mana_of(c, r, i), st, a, stamina_of(st), i, 60))
                                                  for c, races in (("Mage", CASTER), ("Hunter", MELEE)) for r, a, i, st in races]), encoding="utf-8")
        lines = ["\t".join(HEADER)]
        for key, figs in panel._figures.items():
            for (sec, lab), fig in figs.items():
                lines.append("\t".join([key[0], key[1], str(key[2]), sec, lab, str(fig.value), "%" if fig.percent else ""]))
        (ext / "sheet_base.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(rates.main(["fit", "--data-dir", str(self.root / "data")]), 0)
            self.assertEqual(rates.main(["show", "--data-dir", str(self.root / "data")]), 0)
        self.assertIn("Agility per 1% crit and dodge", buf.getvalue())
        self.assertIn("Elf +1%", buf.getvalue())

    def test_cli_without_inputs_exits_two(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(rates.main(["fit", "--data-dir", str(self.root / "none")]), 2)
            self.assertEqual(rates.main(["show", "--data-dir", str(self.root / "none")]), 2)


# --------------------------------------------------------------------------
# The real data (skipped when absent)
# --------------------------------------------------------------------------

REAL_ROOT = Path(os.environ.get("WOWCALC_DATA", REPO / "data"))
REAL_STATS, REAL_PANEL = REAL_ROOT / "external" / "base_stats.tsv", REAL_ROOT / "external" / "sheet_base.tsv"


@unittest.skipUnless(REAL_STATS.is_file() and REAL_PANEL.is_file(), "needs base_stats.tsv and sheet_base.tsv")
class RealDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from wowcalc.basestats import load_base_stats
        from wowcalc.sheetbase import load_sheet_base
        cls.stats = load_base_stats(REAL_STATS)
        cls.panel = load_sheet_base(REAL_PANEL)
        cls.table = fit_rates(cls.stats, cls.panel)

    def test_everything_fits_without_problems(self):
        self.assertEqual(self.table.problems, [])

    def test_the_night_elf_dodge_bonus_is_found(self):
        self.assertEqual(self.table.racial_dodge, {"Night Elf": F(1)})

    def test_the_intellect_rate_is_consistent_with_the_in_game_reading(self):
        # Heart of the Wild 5/5 took a Druid from Intellect 100 to 110 and spell crit from 7.68 to 7.85 (+0.17).
        gain = self.table.intellect_spell_crit.mid * 10
        self.assertEqual(r2(F(768, 100) + gain), F(785, 100))
        for rate in (self.table.intellect_spell_crit.lo, self.table.intellect_spell_crit.hi):
            self.assertIn(r2(F(768, 100) + rate * 10), (F(784, 100), F(785, 100), F(786, 100)))

    def test_the_fitted_rates_reproduce_every_panel_value(self):
        """With the chosen midpoints and ONE constant per class, every race rounds to its displayed value
        (to within one display step for spell crit, which combines two rounded numbers)."""
        for cls in {c for c, _ in self.stats.combos()}:
            races = [r for c, r in self.stats.combos() if c == cls]
            agi = self.table.agility[cls].mid
            for target, key, extra in (("melee", ("melee", "Crit Chance"), None), ):
                consts = []
                for race in races:
                    b = self.stats.at(cls, race, 60)
                    shown = F(str(self.panel.at(cls, race, 60)[key].value))
                    consts.append(shown - agi * b["agility"].value)
                self.assertLessEqual(max(consts) - min(consts), F(1, 100), cls)

    def test_attack_power_rules_reproduce_every_value_exactly(self):
        for cls, race in self.stats.combos():
            rule = self.table.attack_power[cls]
            b = self.stats.at(cls, race, 60)
            expected = rule.per_strength * b["strength"].value + rule.per_agility * b["agility"].value + rule.constant
            self.assertEqual(int(self.panel.at(cls, race, 60)[("melee", "Attack Power")].value), expected, (cls, race))

    def test_hunters_and_rogues_get_attack_power_from_agility_the_others_do_not(self):
        self.assertEqual({c for c, r in self.table.attack_power.items() if r.per_agility}, {"Hunter", "Rogue"})

    def test_armor_is_two_per_agility_and_the_formula_holds(self):
        self.assertEqual(self.table.armor.per_agility, 2)
        self.assertLess(self.table.armor.worst_dr_error, F(5, 1000))

    def test_health_and_mana_rules_and_the_two_race_factors(self):
        self.assertEqual((self.table.health.per_point, self.table.mana.per_point), (10, 15))
        self.assertEqual(self.table.health.race_factors, {"Undead": F(21, 20)})
        self.assertEqual(self.table.mana.race_factors, {"Gnome": F(21, 20)})

    def test_the_pool_rules_reproduce_every_base_health_and_mana_exactly(self):
        for cls, race in self.stats.combos():
            b = self.stats.at(cls, race, 60)
            self.assertEqual(self.table.health.total(cls, race, b["stamina"].value), b["health"].value, (cls, race))
            if b["mana"].value is not None:
                self.assertEqual(self.table.mana.total(cls, race, b["intellect"].value), b["mana"].value, (cls, race))

    def test_outside_references_agree_with_the_panel(self):
        refs = REAL_ROOT / "external" / "rate_references.json"
        if not refs.is_file():
            self.skipTest("no rate_references.json")
        table = fit_rates(self.stats, self.panel)
        apply_references(table, json.loads(refs.read_text(encoding="utf-8")))
        self.assertEqual(table.problems, [])
        self.assertEqual(len(table.agility_points), 9)
        self.assertEqual(len(table.intellect_points), 7)

    def test_the_vanilla_dodge_rates_for_rogues_and_hunters_are_refuted_by_the_panel(self):
        # Vanilla sources give Rogues 14.5 and Hunters 26.5 Agility per 1% dodge. In this build dodge follows the crit
        # rate (about 29 and 53); the fit's dodge check passes only because of that.
        for cls, vanilla in (("Rogue", F("14.5")), ("Hunter", F("26.5"))):
            self.assertNotIn(1 / vanilla, self.table.agility[cls], cls)

    def test_the_agility_rate_is_about_twenty_for_most_classes(self):
        for cls in ("Druid", "Priest", "Warlock", "Warrior"):
            self.assertIn(F(1, 20), self.table.agility[cls], cls)
        self.assertIn(F(1, 53), self.table.agility["Hunter"])


if __name__ == "__main__":
    unittest.main()
