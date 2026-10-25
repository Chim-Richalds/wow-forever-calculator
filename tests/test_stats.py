"""Offline tests for wowcalc.stats. Run: python -m unittest discover -s tests"""

import math
import sys
import unittest
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from effect_fixtures import add_curve, add_effect, write_effects  # noqa: E402
from fixtures import TempCase  # noqa: E402
from basestats_fixtures import row, tsv, warrior  # noqa: E402
from wowcalc.basestats import parse_base_stats  # noqa: E402
from wowcalc.dataset import Dataset  # noqa: E402
from wowcalc.effects import AP_FROM_STAT, SP_FROM_STAT, SHEET_ADD, SCRIPT, SPELL_MODIFIER, STAT_PERCENT, UNSUPPORTED, EffectBook, TalentEffect  # noqa: E402
from fractions import Fraction as F  # noqa: E402
from wowcalc.derived import ArmorRule, AssumedRules, PoolRule  # noqa: E402
from wowcalc.rates import AttackPowerRule, Interval, RateTable  # noqa: E402
from wowcalc.stats import ADJUSTABLE  # noqa: E402
from wowcalc.sheetbase import HEADER, SheetBase  # noqa: E402
from wowcalc.stats import RankedTalent, apply_percents, build_sheet, ranked  # noqa: E402
from wowcalc.trees import load_model  # noqa: E402

# In-game observations (October 2026): total Intellect with the talent at ranks 0..5.
HEART_OF_THE_WILD_BASE_100 = [100, 102, 104, 106, 108, 110]
MENTAL_STRENGTH_BASE_120 = [120, 122, 124, 127, 129, 132]


def stat_talent(label, stat, per_rank, ranks, max_ranks=5):
    effect = TalentEffect("s", 0, 137, STAT_PERCENT, (stat,), {r: per_rank * r for r in range(1, max_ranks + 1)})
    return RankedTalent("n", "s", label, ranks, (effect,))


def priest_table(intellect=120, **kw):
    return parse_base_stats(tsv(row("Priest", "Human", l60=(1500, 2000, 60, 60, 60, intellect, 60), **kw)))


class ApplyPercentsTests(unittest.TestCase):
    def test_heart_of_the_wild_as_observed_in_game(self):
        self.assertEqual([apply_percents(100, [2 * r]) for r in range(6)], HEART_OF_THE_WILD_BASE_100)

    def test_mental_strength_as_observed_in_game(self):
        self.assertEqual([apply_percents(120, [2 * r]) for r in range(6)], MENTAL_STRENGTH_BASE_120)

    def test_the_game_rounds_down_not_to_nearest(self):
        self.assertEqual(apply_percents(120, [4]), 124)  # 124.8
        self.assertEqual(apply_percents(120, [8]), 129)  # 129.6
        self.assertNotEqual([round(120 * (1 + 2 * r / 100)) for r in range(6)], MENTAL_STRENGTH_BASE_120)

    def test_plain_floating_point_would_get_these_wrong(self):
        for base, pct, right in ((100, 15, 115), (25, 16, 29), (50, 16, 58), (180, 15, 207)):
            self.assertEqual(apply_percents(base, [pct]), right)
            self.assertEqual(math.floor(base * (1 + pct / 100)), right - 1)  # the trap this avoids

    def test_several_percentages_multiply(self):
        self.assertEqual(apply_percents(100, [10, 10]), 121)  # ASSUMED stacking rule; adding would give 120

    def test_penalties(self):
        self.assertEqual(apply_percents(100, [-1]), 99)
        self.assertEqual(apply_percents(50, [-1]), 49)  # 49.5 rounds down

    def test_nothing_to_apply(self):
        self.assertEqual((apply_percents(77, []), apply_percents(77, [0]), apply_percents(0, [50])), (77, 77, 0))

    def test_accepts_strings_and_decimals(self):
        self.assertEqual(apply_percents(100, ["15", Decimal("0")]), 115)


class SheetTests(unittest.TestCase):
    def sheet(self, talents, table=None, adjustments=None, level=60):
        return build_sheet(table or priest_table(), "Priest", "Human", level, talents, adjustments)

    def test_mental_strength_reproduces_the_observed_values(self):
        totals = [self.sheet([stat_talent("Mental Strength", "intellect", 2, r)]).lines["intellect"].total for r in range(6)]
        self.assertEqual(totals, MENTAL_STRENGTH_BASE_120)

    def test_heart_of_the_wild_reproduces_the_observed_values(self):
        table = priest_table(intellect=100)
        totals = [self.sheet([stat_talent("Heart of the Wild", "intellect", 2, r)], table).lines["intellect"].total for r in range(6)]
        self.assertEqual(totals, HEART_OF_THE_WILD_BASE_100)

    def test_the_talent_change_is_total_minus_total_without(self):
        line = self.sheet([stat_talent("Mental Strength", "intellect", 2, 3)]).lines["intellect"]
        self.assertEqual((line.base, line.total, line.talent_change), (120, 127, 7))
        self.assertEqual(line.contributions, (("Mental Strength", 6.0),))

    def test_a_manual_adjustment_goes_in_before_the_percentage(self):
        line = self.sheet([stat_talent("Mental Strength", "intellect", 2, 3)], adjustments={"intellect": 20}).lines["intellect"]
        self.assertEqual((line.adjustment, line.total, line.talent_change), (20, 148, 8))  # floor(140 x 1.06)

    def test_two_talents_on_one_stat_multiply(self):
        t = [stat_talent("A", "intellect", 10, 1), stat_talent("B", "intellect", 10, 1)]
        self.assertEqual(self.sheet(t, priest_table(intellect=100)).lines["intellect"].total, 121)

    def test_other_stats_are_untouched(self):
        sheet = self.sheet([stat_talent("Mental Strength", "intellect", 2, 5)])
        for stat in ("strength", "agility", "stamina", "spirit", "health", "mana"):
            line = sheet.lines[stat]
            self.assertEqual((line.total, line.talent_change), (line.base, 0), stat)

    def test_a_talent_that_affects_several_stats(self):
        effect = TalentEffect("s", 0, 137, STAT_PERCENT, ("strength", "stamina"), {1: 10.0})
        sheet = self.sheet([RankedTalent("n", "s", "Both", 1, (effect,))])
        self.assertEqual((sheet.lines["strength"].total, sheet.lines["stamina"].total, sheet.lines["agility"].total), (66, 66, 60))

    def test_health_and_mana_pass_through_and_can_be_adjusted(self):
        sheet = self.sheet([], adjustments={"health": 100})
        self.assertEqual((sheet.lines["health"].total, sheet.lines["mana"].total), (1600, 2000))

    def test_classes_without_mana_have_no_mana_line_value(self):
        sheet = build_sheet(parse_base_stats(tsv(warrior("Human"))), "Warrior", "Human", 60, [])
        line = sheet.lines["mana"]
        self.assertEqual((line.base, line.total, line.talent_change), (None, None, None))

    def test_ranks_of_zero_are_ignored(self):
        sheet = self.sheet([stat_talent("Unspent", "intellect", 2, 0)])
        self.assertEqual((sheet.lines["intellect"].total, sheet.not_modelled), (120, []))

    def test_estimated_bases_are_flagged(self):
        at_30 = self.sheet([], level=30)
        at_60 = self.sheet([], level=60)
        self.assertFalse(at_30.lines["strength"].base_exact)
        self.assertTrue(at_60.lines["strength"].base_exact)

    def test_a_bad_request_raises(self):
        with self.assertRaises(KeyError):
            build_sheet(priest_table(), "Priest", "Orc", 60, [])
        with self.assertRaises(ValueError):
            build_sheet(priest_table(), "Priest", "Human", 61, [])


class NotModelledTests(unittest.TestCase):
    def listed(self, *effects, ranks=2):
        sheet = build_sheet(priest_table(), "Priest", "Human", 60, [RankedTalent("n", "s", "T", ranks, tuple(effects))])
        return sheet

    def test_a_fully_modelled_talent_is_not_listed(self):
        self.assertEqual(self.listed(TalentEffect("s", 0, 137, STAT_PERCENT, ("intellect",), {1: 1.0, 2: 2.0})).not_modelled, [])

    def test_the_unmodelled_part_of_a_partly_modelled_talent_is_listed(self):
        e0 = TalentEffect("s", 0, 137, STAT_PERCENT, ("intellect",), {1: 2.0, 2: 4.0})
        e1 = TalentEffect("s", 1, 4, SCRIPT, (), {1: 4.0, 2: 8.0}, "implemented in game code")
        sheet = self.listed(e0, e1)
        self.assertEqual(sheet.lines["intellect"].total, 124)  # the modelled part still applies
        (item,) = sheet.not_modelled
        self.assertEqual((item.label, item.ranks, item.parts), ("T", 2, ((1, SCRIPT, "implemented in game code"),)))

    def test_spell_modifiers_and_unsupported_effects_are_listed(self):
        sheet = self.listed(TalentEffect("s", 0, 108, SPELL_MODIFIER, (), {}, "changes particular spells"),
                            TalentEffect("s", 1, 79, UNSUPPORTED, (), {}, "aura 79 is not decoded yet"))
        self.assertEqual([p[1] for p in sheet.not_modelled[0].parts], [SPELL_MODIFIER, UNSUPPORTED])

    def test_a_missing_rank_value_is_listed_not_guessed(self):
        sheet = self.listed(TalentEffect("s", 0, 137, STAT_PERCENT, ("intellect",), {1: 2.0}), ranks=2)
        self.assertEqual(sheet.lines["intellect"].total, 120)
        self.assertIn("no value recorded for rank 2", sheet.not_modelled[0].parts[0][2])

    def test_a_talent_with_no_effects_at_all_is_listed(self):
        sheet = self.listed()
        self.assertEqual(sheet.not_modelled[0].parts[0][1:], ("none", "the talent has no effects in the data"))


def figure_talent(label, figures, values, ranks):
    effect = TalentEffect("s", 0, 49, SHEET_ADD, (), dict(values), figures=tuple(figures))
    return RankedTalent("n", "s", label, ranks, (effect,))


def ap_talent(label, stat, values, ranks):
    effect = TalentEffect("s", 0, 598, AP_FROM_STAT, (stat,), dict(values))
    return RankedTalent("n", "s", label, ranks, (effect,))


class FigureAndAttackPowerTests(unittest.TestCase):
    def sheet(self, talents, intellect=87):
        return build_sheet(priest_table(intellect), "Priest", "Human", 60, talents)

    def test_additive_figures_report_the_talent_bonus(self):
        # Feral Swiftness: dodge 4.15 > 6.15 > 8.15 in game, so +0, +2, +4
        for ranks, bonus in ((0, 0), (1, 2), (2, 4)):
            sheet = self.sheet([figure_talent("Feral Swiftness", ["dodge"], {1: 2.0, 2: 4.0}, ranks)])
            self.assertEqual(sheet.figures["dodge"].talent_bonus, bonus)
            self.assertEqual(sheet.figures["parry"].talent_bonus, 0)
        self.assertEqual(sheet.not_modelled, [])

    def test_one_effect_can_raise_two_figures(self):
        sheet = self.sheet([figure_talent("Nature's Majesty", ["melee_crit", "spell_crit"], {1: 2.0, 2: 4.0}, 2)])
        self.assertEqual((sheet.figures["melee_crit"].talent_bonus, sheet.figures["spell_crit"].talent_bonus), (4, 4))

    def test_additive_figures_add_across_talents_and_do_not_multiply(self):
        sheet = self.sheet([figure_talent("A", ["parry"], {1: 5.0}, 1), figure_talent("B", ["parry"], {1: 6.0}, 1)])
        self.assertEqual(sheet.figures["parry"].talent_bonus, 11)

    def test_deflection_parry_ladder(self):
        # Hunter parry 5 > 6 > 7 > 8 > 9 > 10 in game
        vals = {r: float(r) for r in range(1, 6)}
        bonuses = [self.sheet([figure_talent("Deflection", ["parry"], vals, r)]).figures["parry"].talent_bonus for r in range(6)]
        self.assertEqual([5 + int(b) for b in bonuses], [5, 6, 7, 8, 9, 10])

    def test_mental_dexterity_matches_the_in_game_attack_power(self):
        # Intellect 87: Attack Power 276 > 304 > 334 > 363, i.e. +0, +28, +58, +87 (rounded down)
        vals = {1: 33.0, 2: 67.0, 3: 100.0}
        got = [self.sheet([ap_talent("Mental Dexterity", "intellect", vals, r)]).attack_power.bonus for r in range(4)]
        self.assertEqual([276 + g for g in got], [276, 304, 334, 363])

    def test_rounding_down_is_what_the_readings_show(self):
        self.assertEqual(math.floor(Decimal(87) * Decimal(33) / 100), 28)  # 28.71
        self.assertEqual(round(87 * 0.33), 29)  # nearest would have shown 29, not 28

    def test_the_text_66_would_have_been_wrong(self):
        self.assertEqual(math.floor(Decimal(87) * Decimal(66) / 100), 57)  # the game showed +58

    def test_attack_power_uses_the_stat_after_percent_talents(self):
        # ASSUMED: Intellect 120 with +4% is 124, and 100% of that is 124 Attack Power
        sheet = build_sheet(priest_table(120), "Priest", "Human", 60,
                            [stat_talent("Mental Strength", "intellect", 2, 2), ap_talent("MD", "intellect", {3: 100.0}, 3)])
        self.assertEqual(sheet.attack_power.bonus, 124)

    def test_two_attack_power_talents_on_one_stat_add_their_percents_before_rounding(self):
        # ASSUMED rule. 33% + 33% of 87 = 57.42 -> 57; rounding each separately would give 28 + 28 = 56.
        sheet = self.sheet([ap_talent("A", "intellect", {1: 33.0}, 1), ap_talent("B", "intellect", {1: 33.0}, 1)])
        self.assertEqual(sheet.attack_power.bonus, 57)

    def test_attack_power_from_different_stats_is_summed(self):
        table = parse_base_stats(tsv(row("Priest", "Human", l60=(1500, 2000, 80, 60, 60, 87, 60))))
        sheet = build_sheet(table, "Priest", "Human", 60, [ap_talent("A", "intellect", {1: 100.0}, 1), ap_talent("B", "strength", {1: 50.0}, 1)])
        self.assertEqual(sheet.attack_power.bonus, 87 + 40)

    def test_attack_power_with_no_talents_is_zero(self):
        self.assertEqual(self.sheet([]).attack_power.bonus, 0)

    def test_a_missing_rank_is_listed_not_guessed(self):
        sheet = self.sheet([figure_talent("X", ["dodge"], {1: 2.0}, 2)])
        self.assertEqual(sheet.figures["dodge"].talent_bonus, 0)
        self.assertEqual([n.label for n in sheet.not_modelled], ["X"])


def panel(class_name, race, level, **figures):
    """A SheetBase with the named figures, e.g. melee_crit=("melee", "Crit Chance", "4.21", "%")."""
    rows = [dict(zip(HEADER, [class_name, race, str(level), sec, lab, val, unit])) for sec, lab, val, unit in figures.values()]
    return SheetBase(rows)


DRUID_PANEL = dict(melee_crit=("melee", "Crit Chance", "4.21", "%"), spell_crit=("spell", "Spell Crit", "7.68", "%"),
                   melee_hit=("melee", "Hit Chance", "0.00", "%"), spell_hit=("spell", "Spell Hit", "0.00", "%"),
                   dodge=("defense", "Dodge Chance", "4.15", "%"), ap=("melee", "Attack Power", "104", ""))


def power_talent(label, figure, stat, values, ranks):
    effect = TalentEffect("s", 0, 174, SP_FROM_STAT, (stat,), dict(values), figures=(figure,))
    return RankedTalent("n", "s", label, ranks, (effect,))


class SpellPowerFromStatTests(unittest.TestCase):
    def sheet(self, talents, spirit=100, intellect=100, adjust=None):
        table = parse_base_stats(tsv(row("Priest", "Human", l60=(1500, 2000, 60, 60, 60, intellect, spirit))))
        base = panel("Priest", "Human", 60, sp=("spell", "Spell Damage", "0", ""), heal=("spell", "Healing", "0", ""))
        return build_sheet(table, "Priest", "Human", 60, talents, adjustments=adjust, sheet_base=base)

    def test_a_percent_of_the_stat_is_added_rounded_down(self):
        # Spiritual Guidance rank 5 reads 8% to damage and 25% to healing (the data's values)
        sheet = self.sheet([power_talent("SG", "spell_power", "spirit", {5: 8.0}, 5), power_talent("SG", "healing", "spirit", {5: 25.0}, 5)], spirit=117)
        self.assertEqual((sheet.spell_power.from_talents, sheet.healing.from_talents), (9, 29))   # 9.36, 29.25
        self.assertEqual((sheet.spell_power.total, sheet.healing.total), (9, 29))

    def test_it_follows_the_stat_total_after_talent_and_gear_changes(self):
        stat_talent = RankedTalent("m", "t", "Spirit talent", 1, (TalentEffect("t", 0, 137, STAT_PERCENT, ("spirit",), {1: 10.0}),))
        sheet = self.sheet([stat_talent, power_talent("SG", "spell_power", "spirit", {1: 10.0}, 1)], spirit=100, adjust={"spirit": 20})
        self.assertEqual(sheet.lines["spirit"].total, 132)             # (100 + 20) x 1.10
        self.assertEqual(sheet.spell_power.from_talents, 13)           # 13.2

    def test_percents_from_the_same_stat_add_before_rounding_and_other_stats_round_apart(self):
        sheet = self.sheet([power_talent("A", "spell_power", "intellect", {1: 3.0}, 1), power_talent("B", "spell_power", "intellect", {1: 4.0}, 1),
                            power_talent("C", "spell_power", "spirit", {1: 5.0}, 1)], spirit=39, intellect=100)
        self.assertEqual(sheet.spell_power.from_talents, 7 + 1)        # 7% of 100, and floor(1.95)
        self.assertEqual([c[1] for c in sheet.spell_power.contributions], ["intellect", "intellect", "spirit"])

    def test_gear_is_added_on_top_and_unspent_points_add_nothing(self):
        sheet = self.sheet([power_talent("SG", "spell_power", "spirit", {1: 10.0}, 0)], adjust={"spell_power": 50})
        self.assertEqual((sheet.spell_power.from_talents, sheet.spell_power.total), (0, 50))
        self.assertEqual(self.sheet([]).spell_power.contributions, ())

    def test_no_panel_figure_no_total_and_a_note_when_talents_apply(self):
        table = parse_base_stats(tsv(row("Priest", "Human", l60=(1500, 2000, 60, 60, 60, 100, 100))))
        sheet = build_sheet(table, "Priest", "Human", 60, [power_talent("SG", "healing", "spirit", {1: 10.0}, 1)])
        self.assertIsNone(sheet.healing.total)
        self.assertTrue(any("rounded down" in n for n in sheet.notes))
        self.assertFalse(any("rounded down" in n for n in self.sheet([]).notes))


class SpellPowerTests(unittest.TestCase):
    def sheet(self, adjust=None, **kw):
        table = parse_base_stats(tsv(row("Druid", "Tauren", l60=(1500, 2000, 60, 60, 60, 100, 60))))
        return build_sheet(table, "Druid", "Tauren", 60, [], adjustments=adjust, sheet_base=panel("Druid", "Tauren", 60, **kw))

    def test_base_comes_from_the_panel_and_gear_is_added_by_hand(self):
        sp = self.sheet(sp=("spell", "Spell Damage", "12", "")).spell_power
        self.assertEqual((sp.base, sp.item, sp.total), (12, 0, 12))
        sp = self.sheet({"spell_power": 150}, sp=("spell", "Spell Damage", "0", "")).spell_power
        self.assertEqual((sp.base, sp.item, sp.total), (0, 150, 150))

    def test_the_other_panel_figures_are_not_mistaken_for_it(self):
        self.assertIsNone(self.sheet(heal=("spell", "Healing", "40", "")).spell_power.base)

    def test_no_panel_no_total(self):
        table = parse_base_stats(tsv(row("Druid", "Tauren", l60=(1500, 2000, 60, 60, 60, 100, 60))))
        sheet = build_sheet(table, "Druid", "Tauren", 60, [], adjustments={"spell_power": 5})
        self.assertEqual((sheet.spell_power.base, sheet.spell_power.total), (None, None))

    def test_healing_is_its_own_figure(self):
        sheet = self.sheet({"healing": 40}, sp=("spell", "Spell Damage", "0", ""), heal=("spell", "Healing", "3", ""))
        self.assertEqual((sheet.healing.base, sheet.healing.total, sheet.spell_power.total), (3, 43, 0))
        self.assertIsNone(self.sheet(sp=("spell", "Spell Damage", "0", "")).healing.base)

    def test_it_is_an_accepted_adjustment(self):
        self.assertIn("spell_power", ADJUSTABLE)
        self.assertIn("healing", ADJUSTABLE)


class CastingRegenTests(unittest.TestCase):
    def sheet(self, talents, panel_figures=None):
        table = parse_base_stats(tsv(row("Priest", "Human", l60=(1500, 2000, 60, 60, 60, 100, 100))))
        base = panel("Priest", "Human", 60, **(panel_figures or {}))
        return build_sheet(table, "Priest", "Human", 60, talents, sheet_base=base)

    def test_talents_add_up_from_zero_with_a_note(self):
        sheet = self.sheet([figure_talent("Meditation", ["casting_regen"], {3: 50.0}, 3), figure_talent("Other", ["casting_regen"], {1: 10.0}, 1)],
                           dict(spell_crit=("spell", "Spell Crit", "7.00", "%")))
        fig = sheet.figures["casting_regen"]
        self.assertEqual((fig.base, fig.talent_bonus, fig.total), (Decimal(0), Decimal(60), Decimal(60)))
        self.assertTrue(any("mana regeneration while casting" in n for n in sheet.notes))

    def test_without_the_talent_the_figure_has_no_base_and_no_note(self):
        sheet = self.sheet([], dict(spell_crit=("spell", "Spell Crit", "7.00", "%")))
        self.assertIsNone(sheet.figures["casting_regen"].base)
        self.assertFalse(any("while casting" in n for n in sheet.notes))
        self.assertIsNone(self.sheet([figure_talent("M", ["casting_regen"], {1: 17.0}, 0)]).figures["casting_regen"].base)   # no points spent


class ParryTests(unittest.TestCase):
    def sheet(self, parry_talent, with_base, ranks=1):
        table = parse_base_stats(tsv(row("Druid", "Tauren", l60=(1500, 2000, 60, 60, 60, 100, 60))))
        figures = dict(DRUID_PANEL)
        if with_base:
            figures["parry"] = ("defense", "Parry Chance", "5.00", "%")
        talents = [figure_talent("Deflection", ["parry"], {1: 1.0, 2: 2.0}, ranks)] if parry_talent else []
        return build_sheet(table, "Druid", "Tauren", 60, talents, sheet_base=panel("Druid", "Tauren", 60, **figures))

    def test_a_class_that_parries_has_a_base(self):
        self.assertEqual(self.sheet(False, True).figures["parry"].total, Decimal("5.00"))
        self.assertEqual(self.sheet(True, True, 2).figures["parry"].total, Decimal("7.00"))

    def test_a_class_without_parry_has_none_until_a_talent_gives_it(self):
        none = self.sheet(False, False)
        self.assertIsNone(none.figures["parry"].base)
        self.assertFalse(any("parry" in n for n in none.notes))
        self.assertIsNone(self.sheet(True, False, 0).figures["parry"].base)   # talent present but no points spent
        gained = self.sheet(True, False, 2)
        self.assertEqual((gained.figures["parry"].base, gained.figures["parry"].total), (Decimal(0), Decimal("2.0")))
        self.assertTrue(any("counted from 0" in n for n in gained.notes))


class SheetBaseIntegrationTests(unittest.TestCase):
    def druid(self, talents, level=60, **kw):
        table = parse_base_stats(tsv(row("Druid", "Tauren", l60=(1500, 2000, 60, 60, 60, 100, 60))))
        base = panel("Druid", "Tauren", 60, **{**DRUID_PANEL, **kw})
        return build_sheet(table, "Druid", "Tauren", level, talents, sheet_base=base)

    def test_totals_reproduce_the_in_game_readings(self):
        # Nature's Majesty melee crit 4.21 > 6.21 > 8.21; Feral Swiftness dodge 4.15 > 6.15 > 8.15
        for ranks, crit, dodge in ((0, "4.21", "4.15"), (1, "6.21", "6.15"), (2, "8.21", "8.15")):
            sheet = self.druid([figure_talent("NM", ["melee_crit"], {1: 2.0, 2: 4.0}, ranks),
                                figure_talent("FS", ["dodge"], {1: 2.0, 2: 4.0}, ranks)])
            self.assertEqual((sheet.figures["melee_crit"].total, sheet.figures["dodge"].total),
                             (Decimal(crit), Decimal(dodge)), ranks)

    def test_hit_goes_from_zero(self):
        sheet = self.druid([figure_talent("NR", ["melee_hit", "spell_hit"], {1: 2.0, 2: 4.0}, 2)])
        self.assertEqual((sheet.figures["melee_hit"].total, sheet.figures["spell_hit"].total), (4, 4))

    def test_percent_figures_are_added_exactly_not_in_floating_point(self):
        sheet = self.druid([figure_talent("A", ["spell_crit"], {1: 0.1}, 1), figure_talent("B", ["spell_crit"], {1: 0.2}, 1)])
        self.assertEqual(sheet.figures["spell_crit"].total, Decimal("7.98"))  # 7.68 + 0.1 + 0.2

    def test_attack_power_total_includes_the_panel_base(self):
        sheet = self.druid([ap_talent("MD", "intellect", {1: 100.0}, 1)])
        self.assertEqual((sheet.attack_power.base, sheet.attack_power.bonus, sheet.attack_power.total), (104, 100, 204))

    def test_a_figure_with_no_panel_has_no_base_or_total(self):
        table = parse_base_stats(tsv(row("Druid", "Tauren", l60=(1500, 2000, 60, 60, 60, 100, 60))))
        sheet = build_sheet(table, "Druid", "Tauren", 60, [figure_talent("DF", ["parry"], {1: 5.0}, 1)])   # no panel at all
        self.assertIsNone(sheet.figures["parry"].base)
        self.assertIsNone(sheet.figures["parry"].total)
        self.assertEqual(sheet.figures["parry"].talent_bonus, 5)  # the talent's bonus is still reported
        self.assertFalse(any("counted from 0" in n for n in sheet.notes))   # no panel, so nothing says the class cannot parry

    def test_a_level_without_figures_degrades_and_says_so(self):
        table = parse_base_stats(tsv(row("Druid", "Tauren", l60=(1500, 2000, 60, 60, 60, 100, 60))))
        sheet = build_sheet(table, "Druid", "Tauren", 30, [figure_talent("NM", ["melee_crit"], {1: 2.0}, 1)],
                            sheet_base=panel("Druid", "Tauren", 60, **DRUID_PANEL))
        self.assertIsNone(sheet.figures["melee_crit"].total)
        self.assertEqual(sheet.figures["melee_crit"].talent_bonus, 2)
        self.assertTrue(any("not estimated" in n for n in sheet.notes))

    def test_an_unknown_combination_degrades_and_says_so(self):
        table = parse_base_stats(tsv(row("Druid", "Tauren", l60=(1500, 2000, 60, 60, 60, 100, 60))))
        sheet = build_sheet(table, "Druid", "Tauren", 60, [], sheet_base=panel("Mage", "Human", 60, **DRUID_PANEL))
        self.assertTrue(sheet.notes)
        self.assertIsNone(sheet.attack_power.base)

    def test_without_a_panel_file_nothing_changes(self):
        table = parse_base_stats(tsv(row("Druid", "Tauren", l60=(1500, 2000, 60, 60, 60, 100, 60))))
        sheet = build_sheet(table, "Druid", "Tauren", 60, [figure_talent("NM", ["melee_crit"], {1: 2.0}, 1)])
        self.assertEqual((sheet.figures["melee_crit"].base, sheet.figures["melee_crit"].total, sheet.notes), (None, None, []))


def simple_rates():
    """Agility 1/20 per point, Intellect 1/60, Druid attack power 2 per Strength - 20; exact, no uncertainty."""
    table = RateTable(agility={"Druid": Interval(F(1, 20), F(1, 20))}, intellect_spell_crit=Interval(F(1, 60), F(1, 60)),
                      attack_power={"Druid": AttackPowerRule(2, 0, -20)})
    table.spell_classes = frozenset({"Druid"})
    return table


class StatFlowThroughTests(unittest.TestCase):
    def druid(self, talents, adjustments=None, level=60, rates="default", **panel_kw):
        table = parse_base_stats(tsv(row("Druid", "Tauren", l60=(1500, 2000, 70, 60, 60, 100, 60))))
        base = panel("Druid", "Tauren", 60, **{**DRUID_PANEL, **panel_kw})
        return build_sheet(table, "Druid", "Tauren", level, talents, adjustments, base,
                           simple_rates() if rates == "default" else rates)

    def test_heart_of_the_wild_raises_spell_crit_through_intellect(self):
        # In game: Intellect 100 > 110 took spell crit from 7.68 to 7.85 (+0.17).
        sheet = self.druid([stat_talent("Heart of the Wild", "intellect", 2, 5)])
        line = sheet.figures["spell_crit"]
        self.assertEqual(sheet.lines["intellect"].total, 110)
        self.assertEqual(round(line.from_talent_stats, 2), Decimal("0.17"))
        self.assertEqual(round(line.total, 2), Decimal("7.85"))
        self.assertEqual(line.talent_bonus, 0)  # none of it is the talent's own points
        self.assertEqual(sheet.figures["melee_crit"].from_talent_stats, 0)  # Intellect does not move melee crit

    def test_agility_moves_melee_crit_spell_crit_and_dodge(self):
        sheet = self.druid([stat_talent("Lightning Reflexes", "agility", 10, 2, 2)])  # 60 -> 72
        self.assertEqual(sheet.lines["agility"].total, 72)
        for fig in ("melee_crit", "spell_crit", "dodge"):
            self.assertEqual(sheet.figures[fig].from_talent_stats, Decimal("0.6"), fig)  # 12 / 20
        self.assertEqual(sheet.figures["melee_hit"].from_talent_stats, 0)

    def test_extra_strength_raises_attack_power_by_the_class_rule(self):
        sheet = self.druid([stat_talent("Strength talent", "strength", 10, 2, 2)])  # 70 -> 84
        self.assertEqual(sheet.attack_power.from_talent_stats, 28)  # 2 per point
        self.assertEqual(sheet.attack_power.total, 104 + 28)

    def test_adjustments_and_talents_are_kept_apart(self):
        sheet = self.druid([stat_talent("Mental Strength", "intellect", 2, 5)], {"intellect": 40})
        self.assertEqual(sheet.lines["intellect"].total, 154)  # floor(140 x 1.10)
        line = sheet.figures["spell_crit"]
        self.assertEqual(line.from_adjustments, Decimal(40) / 60)
        self.assertEqual(line.from_talent_stats, Decimal(14) / 60)  # the 10% applies to the adjusted stat: +14
        self.assertEqual(line.total, Decimal("7.68") + Decimal(54) / 60)

    def test_adjusted_strength_raises_attack_power_separately(self):
        sheet = self.druid([], {"strength": 10})
        self.assertEqual((sheet.attack_power.from_adjustments, sheet.attack_power.from_talent_stats), (20, 0))
        self.assertEqual(sheet.attack_power.total, 124)

    def test_adjustments_alone_move_the_figures(self):
        sheet = self.druid([], {"agility": 20})
        self.assertEqual(sheet.figures["dodge"].from_adjustments, 1)
        self.assertEqual(sheet.figures["dodge"].from_talent_stats, 0)
        self.assertEqual(sheet.figures["dodge"].total, Decimal("5.15"))

    def test_additive_talent_points_and_stat_effects_add_up(self):
        sheet = self.druid([stat_talent("Lightning Reflexes", "agility", 10, 2, 2), figure_talent("Feral Swiftness", ["dodge"], {2: 4.0}, 2)])
        line = sheet.figures["dodge"]
        self.assertEqual((line.talent_bonus, line.from_talent_stats, line.talent_total), (4, Decimal("0.6"), Decimal("4.6")))
        self.assertEqual(line.total, Decimal("4.15") + Decimal("4.6"))

    def test_attack_power_from_a_stat_talent_and_from_extra_stats_both_count(self):
        sheet = self.druid([ap_talent("Mental Dexterity", "intellect", {1: 100.0}, 1), stat_talent("Strength talent", "strength", 10, 1, 1)])
        self.assertEqual((sheet.attack_power.bonus, sheet.attack_power.from_talent_stats), (100, 14))
        self.assertEqual(sheet.attack_power.talent_total, 114)
        self.assertEqual(sheet.attack_power.total, 104 + 114)

    def test_uncertainty_is_reported_from_the_rate_ranges(self):
        rates = simple_rates()
        rates.agility["Druid"] = Interval(F(49, 1000), F(51, 1000))
        sheet = self.druid([stat_talent("Lightning Reflexes", "agility", 10, 2, 2)], rates=rates)
        self.assertEqual(sheet.figures["dodge"].uncertainty, Decimal(12) * Decimal(1) / Decimal(1000))
        self.assertEqual(self.druid([], rates=rates).figures["dodge"].uncertainty, 0)

    def test_without_rates_extra_stats_change_nothing(self):
        sheet = self.druid([stat_talent("Mental Strength", "intellect", 2, 5)], rates=None)
        self.assertEqual(sheet.figures["spell_crit"].from_talent_stats, 0)
        self.assertEqual(sheet.figures["spell_crit"].total, Decimal("7.68"))

    def test_rates_are_not_applied_away_from_their_level(self):
        rates = simple_rates()
        rates.level = 59
        sheet = self.druid([stat_talent("Mental Strength", "intellect", 2, 5)], rates=rates)
        self.assertEqual(sheet.figures["spell_crit"].from_talent_stats, 0)
        self.assertTrue(any("fitted at level 59" in n for n in sheet.notes))

    def test_a_class_without_rates_says_so(self):
        rates = simple_rates()
        rates.agility.clear()
        sheet = self.druid([stat_talent("Mental Strength", "intellect", 2, 5)], rates=rates)
        self.assertTrue(any("no fitted rates for Druid" in n for n in sheet.notes))
        self.assertEqual(sheet.figures["spell_crit"].from_talent_stats, 0)

    def test_a_class_with_no_spell_crit_gets_no_intellect_effect(self):
        rates = simple_rates()
        rates.spell_classes = frozenset()
        sheet = self.druid([stat_talent("Mental Strength", "intellect", 2, 5)], rates=rates)
        self.assertEqual(sheet.figures["spell_crit"].from_talent_stats, 0)


def full_rates():
    """simple_rates plus armor 2 per Agility, health 10 per Stamina (+5% for Tauren), mana 15 per Intellect, block assumptions."""
    table = simple_rates()
    table.armor = ArmorRule(2, F(0))
    table.health = PoolRule(10, {"Druid": 900}, {"Tauren": F(21, 20)})
    table.mana = PoolRule(15, {"Druid": 500}, {})
    table.assumed = AssumedRules(F(5), ("Druid", "Warrior"), 20)
    return table


class ResourceTests(unittest.TestCase):
    """Armor, health, mana and block. Druid / Tauren, level 60: Strength 70, Agility 60, Stamina 60, Intellect 100."""
    HEALTH = (900 + 600) * 21 // 20   # 1575
    MANA = 500 + 1500                 # 2000

    def druid(self, talents=(), adjustments=None, rates="default", shield=False, level=60, cls="Druid"):
        table = parse_base_stats(tsv(row("Druid", "Tauren", l60=(self.HEALTH, self.MANA, 70, 60, 60, 100, 60)),
                                     row("Warrior", "Tauren", l60=(self.HEALTH, self.MANA, 70, 60, 60, 100, 60))))
        base = SheetBase([dict(zip(HEADER, [c, "Tauren", "60", sec, lab, val, unit]))
                          for c in ("Druid", "Warrior") for sec, lab, val, unit in (("defense", "Armor", "120", ""), ("melee", "Attack Power", "100", ""))])
        return build_sheet(table, cls, "Tauren", level, list(talents), adjustments, base, full_rates() if rates == "default" else rates, shield)

    # ---- armor
    def test_armor_is_two_per_agility_plus_gear_and_gives_damage_reduction(self):
        sheet = self.druid([stat_talent("Agility talent", "agility", 10, 2, 2)], {"agility": 10, "armor": 1000})
        a = sheet.armor
        self.assertEqual((a.base, a.from_adjustments, a.item_armor), (120, 20, 1000))
        self.assertEqual(a.from_talent_stats, 2 * (sheet.lines["agility"].total - 60 - 10))
        self.assertEqual(a.total, 120 + a.from_talent_stats + 20 + 1000)
        self.assertEqual(round(a.damage_reduction, 2), round(Decimal(100 * a.total) / (a.total + 85 * 60 + 400), 2))

    def test_the_armor_formula_matches_the_panel(self):
        # Orc Shaman, level 60: armor 104 shows 1.86%
        self.assertEqual(round(full_rates().armor.damage_reduction(104, 60)), 2)
        self.assertEqual(round(float(full_rates().armor.damage_reduction(104, 60)), 2), 1.86)

    def test_armor_without_rates_is_the_panel_figure_plus_gear_and_no_damage_reduction(self):
        a = self.druid(adjustments={"armor": 50}, rates=None).armor
        self.assertEqual((a.base, a.total, a.damage_reduction), (120, 170, None))

    def test_no_armor_without_a_panel(self):
        table = parse_base_stats(tsv(row("Druid", "Tauren", l60=(self.HEALTH, self.MANA, 70, 60, 60, 100, 60))))
        sheet = build_sheet(table, "Druid", "Tauren", 60, [], None, None, full_rates())
        self.assertEqual((sheet.armor.base, sheet.armor.total, sheet.armor.damage_reduction), (None, None, None))

    # ---- health and mana
    def test_extra_stamina_raises_health_by_ten_times_the_race_factor(self):
        # Stamina 60 -> 90 (+50% in three steps is overkill; use a flat adjustment): 30 x 10 x 1.05 = 315
        sheet = self.druid(adjustments={"stamina": 30})
        self.assertEqual(sheet.lines["health"].total, (900 + 10 * 90) * 21 // 20)
        self.assertEqual(sheet.lines["health"].talent_change, sheet.lines["health"].total - self.HEALTH)

    def test_without_extra_stats_health_and_mana_are_the_base_values(self):
        sheet = self.druid()
        self.assertEqual((sheet.lines["health"].total, sheet.lines["mana"].total), (self.HEALTH, self.MANA))

    def test_extra_intellect_raises_mana_by_fifteen(self):
        sheet = self.druid([stat_talent("Arcane Mind", "intellect", 2, 5)])  # 100 -> 110
        self.assertEqual(sheet.lines["mana"].total, 500 + 15 * 110)

    def test_a_manual_health_adjustment_is_added_after_the_rule(self):
        self.assertEqual(self.druid(adjustments={"health": 100}).lines["health"].total, self.HEALTH + 100)

    def test_a_rule_that_does_not_reproduce_the_base_is_not_applied_and_says_so(self):
        rates = full_rates()
        rates.health = PoolRule(10, {"Druid": 123}, {})
        sheet = self.druid(adjustments={"stamina": 30}, rates=rates)
        self.assertEqual(sheet.lines["health"].total, self.HEALTH)
        self.assertTrue(any("health rule does not reproduce" in n for n in sheet.notes))

    def test_a_class_without_a_pool_rule_keeps_the_plain_value(self):
        sheet = self.druid(adjustments={"stamina": 30}, cls="Warrior")  # the fixture rules only cover Druid
        self.assertEqual(sheet.lines["health"].total, self.HEALTH)

    def test_pools_are_not_recomputed_at_another_level(self):
        sheet = self.druid(adjustments={"stamina": 30}, level=30)
        self.assertEqual(sheet.lines["health"].total, sheet.lines["health"].base)  # the estimate, untouched by Stamina

    # ---- block
    def test_block_needs_a_shield_and_starts_at_the_assumed_base(self):
        self.assertIsNone(self.druid().figures["block"].base)
        sheet = self.druid(shield=True)
        self.assertEqual(sheet.figures["block"].base, 5)
        self.assertTrue(any("confirmed from the game" in n for n in sheet.notes))

    def test_block_talents_add_to_the_base(self):
        sheet = self.druid([figure_talent("Shield Specialization", ["block"], {1: 1.0, 5: 5.0}, 5)], shield=True)
        self.assertEqual((sheet.figures["block"].talent_bonus, sheet.figures["block"].total), (5, 10))

    def test_block_talent_without_a_shield_shows_the_bonus_but_no_total(self):
        sheet = self.druid([figure_talent("Shield Specialization", ["block"], {5: 5.0}, 5)])
        self.assertEqual((sheet.figures["block"].talent_bonus, sheet.figures["block"].total), (5, None))

    def test_strength_gives_block_value_not_block_chance(self):
        sheet = self.druid([stat_talent("Strength talent", "strength", 10, 2, 2)], shield=True)  # 70 -> 84
        self.assertEqual(sheet.block_value_from_strength, 84 // 20)
        self.assertEqual(sheet.figures["block"].total, 5)  # unchanged by Strength
        self.assertTrue(any("does not raise block chance" in n for n in sheet.notes))

    def test_a_class_that_cannot_use_shields_gets_no_block(self):
        rates = full_rates()
        rates.assumed = AssumedRules(F(5), ("Warrior",), 20)
        sheet = self.druid(shield=True, rates=rates)
        self.assertIsNone(sheet.figures["block"].base)
        self.assertIsNone(sheet.block_value_from_strength)
        self.assertTrue(any("not one of" in n for n in sheet.notes))

    def test_shield_without_rates_says_so(self):
        sheet = self.druid(shield=True, rates=None)
        self.assertIsNone(sheet.figures["block"].base)
        self.assertTrue(any("no rates file" in n for n in sheet.notes))

    # ---- adjustments
    def test_an_unknown_adjustment_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self.druid(adjustments={"inteligence": 20})
        self.assertIn("inteligence", str(ctx.exception))
        self.assertIn("armor", str(ctx.exception))


class IntegrationTests(TempCase):
    def test_decoded_from_data_to_a_stat_table(self):
        def mutate(t):
            add_effect(t, 1, misc1=8)
            add_curve(t, 1, 0, (2, 4))
        ds = Dataset(write_effects(self.root / "ds", mutate), "t")
        model = load_model(ds)
        book = EffectBook.for_model(ds, model)
        node = model.by_name("Mage").nodes["1"]
        table = parse_base_stats(tsv(row("Mage", "Human")))  # intellect 20 at level 1, 60 at level 60
        totals = [build_sheet(table, "Mage", "Human", 60, [ranked(book, node, "Talent 1", r)]).lines["intellect"].total for r in (0, 1, 2)]
        self.assertEqual(totals, [60, 61, 62])  # floor(60 x 1.02) = 61, floor(60 x 1.04) = 62


if __name__ == "__main__":
    unittest.main()
