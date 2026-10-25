"""Offline tests for wowcalc.talentinfo. Run: python -m unittest discover -s tests"""

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from effect_fixtures import add_curve, add_effect, effect_tables  # noqa: E402
from fixtures import TempCase, write_dataset  # noqa: E402
from basestats_fixtures import row, tsv  # noqa: E402
from wowcalc.dataset import Dataset  # noqa: E402
from wowcalc.fetch import TABLES  # noqa: E402
from wowcalc.sheetbase import HEADER  # noqa: E402
from wowcalc.talentinfo import find_talents, main, spell_names  # noqa: E402
from wowcalc.trees import load_model  # noqa: E402

BUILD = "1.60.1.70450"


def with_effects(t):
    add_effect(t, 1, misc1=8)                      # node 1, 2 ranks: +2% / +4% Intellect
    add_curve(t, 1, 0, (2, 4))
    add_effect(t, 2, index=0, aura=108, base=5)    # node 2: a spell modifier
    add_effect(t, 2, index=1, aura=290, base=1, misc1=0)  # ... and +crit (2 / 4 points, melee and spell)
    add_curve(t, 2, 1, (2, 4))
    add_effect(t, 5, index=0, misc1=2)             # node 5, 2 ranks: Agility, plus a script effect
    add_curve(t, 5, 0, (1, 2))
    add_effect(t, 5, index=1, aura=4)
    add_curve(t, 5, 1, (7, 9))
    for spell in t["Spell"][1]:
        if spell["ID"] == "3005":
            spell["Description_lang"] = "Agility by $s1%; in a form by $s2%."


class InfoCase(TempCase):
    def setUp(self):
        super().setUp()
        self.data = self.root / "data"

    def install(self, complete=True, base_stats=True):
        directory = write_dataset(self.data / "raw" / BUILD, effect_tables(with_effects))
        if complete:  # stand-ins for the tables these tests do not use
            for table in TABLES:
                if not (directory / f"{table}.csv").exists():
                    (directory / f"{table}.csv").write_text("ID\n1\n", encoding="utf-8")
        else:
            (directory / "TraitCost.csv").unlink()
        if base_stats:
            (self.data / "external").mkdir(parents=True, exist_ok=True)
            (self.data / "external" / "base_stats.tsv").write_text(
                tsv(row("Mage", "Human", l60=(1500, 2000, 60, 60, 60, 60, 60)), row("Warrior", "Orc")), encoding="utf-8")

    def write_sheet_base(self, level, melee_crit=None):
        rows = [["Mage", "Human", str(level), "melee", "Attack Power", "100", ""]]
        if melee_crit:
            rows.append(["Mage", "Human", str(level), "melee", "Crit Chance", melee_crit, "%"])
            rows.append(["Mage", "Human", str(level), "spell", "Spell Crit", "7.00", "%"])
            rows.append(["Mage", "Human", str(level), "spell", "Spell Damage", "0", ""])
        (self.data / "external").mkdir(parents=True, exist_ok=True)
        (self.data / "external" / "sheet_base.tsv").write_text(
            "\n".join("\t".join(r) for r in [HEADER, *rows]) + "\n", encoding="utf-8")

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--build", BUILD, "--data-dir", str(self.data), *args])
        return code, out.getvalue()


class FindTests(InfoCase):
    def setUp(self):
        super().setUp()
        self.install()
        ds = Dataset(self.data / "raw" / BUILD, BUILD)
        self.model, self.names = load_model(ds), spell_names(ds)

    def test_exact_name_ignoring_case(self):
        found = find_talents(self.model, self.names, "talent 1")
        self.assertEqual([(t.class_name, n.id) for t, n in found], [("Mage", "1")])

    def test_exact_beats_substring(self):
        self.assertEqual(len(find_talents(self.model, self.names, "Talent 1")), 1)  # not also "Talent 10"-like matches

    def test_substring_when_there_is_no_exact_match(self):
        self.assertEqual(len(find_talents(self.model, self.names, "Talent")), 7)

    def test_class_filter(self):
        self.assertEqual([n.id for _, n in find_talents(self.model, self.names, "Talent", "warrior")], ["7"])

    def test_no_match(self):
        self.assertEqual(find_talents(self.model, self.names, "Nothing"), [])


class InfoCommandTests(InfoCase):
    def test_describes_effects_ranks_and_the_ladder(self):
        self.install()
        code, out = self.run_cli("Talent 1", "--base", "100")
        self.assertEqual(code, 0)
        self.assertIn("Talent 1 (Mage): node 1, spell 3001, definition 2001, max ranks 2", out)
        self.assertIn("effect 0: aura 137, stat_percent -> intellect", out)
        self.assertIn("value by rank: 1: 2, 2: 4", out)
        self.assertIn("rank 2: Does thing 1.", out)
        self.assertIn("with a base of 100 intellect, ranks 0..2: 100 > 102 > 104", out)

    def test_tooltips_are_rendered_per_rank_with_each_effects_own_value(self):
        self.install()
        _, out = self.run_cli("Talent 5")
        self.assertIn("rank 1: Agility by 1%; in a form by 7%.", out)
        self.assertIn("rank 2: Agility by 2%; in a form by 9%.", out)
        self.assertIn("effect 1: aura 4, script", out)

    def test_unmodelled_effects_are_shown_with_their_kind_but_get_no_ladder(self):
        self.install()
        _, out = self.run_cli("Talent 2", "--base", "100")
        self.assertIn("aura 108, spell_modifier", out)
        self.assertNotIn("with a base of", out)

    def test_several_talents_and_a_miss(self):
        self.install()
        code, out = self.run_cli("Talent 1", "Nonexistent", "Talent 7")
        self.assertEqual(code, 1)
        self.assertIn("No talent matches 'Nonexistent'.", out)
        self.assertIn("Talent 1 (Mage)", out)
        self.assertIn("Talent 7 (Warrior)", out)

    def test_class_option(self):
        self.install()
        self.assertEqual(self.run_cli("Talent", "--class", "Warrior")[1].count("(Warrior)"), 1)

    def test_an_incomplete_build_points_to_complete(self):
        self.install(complete=False)
        code, out = self.run_cli("Talent 1")
        self.assertEqual(code, 2)
        self.assertIn("python -m wowcalc.update complete", out)

    def test_nothing_installed_and_no_arguments(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--data-dir", str(self.root / "empty"), "Talent 1"]), 2)
            self.assertEqual(main(["--data-dir", str(self.root / "empty")]), 2)


class SheetCommandTests(InfoCase):
    def sheet(self, *args):
        return self.run_cli("sheet", "--class", "Mage", "--race", "Human", *args)

    def test_a_stat_table_with_the_talent_applied(self):
        self.install()
        code, out = self.sheet("--talent", "Talent 1=2")
        self.assertEqual(code, 0)
        self.assertIn("Human Mage, level 60", out)
        line = next(l for l in out.splitlines() if l.startswith("intellect"))
        self.assertIn("62", line)
        self.assertIn("+2", line)
        self.assertIn("Talent 1 +4%", line)
        self.assertNotIn("not a legal build", out)  # two points in a root talent is a legal build

    def test_without_a_sheet_figures_file_it_says_so_and_shows_only_bonuses(self):
        self.install()
        _, out = self.sheet("--talent", "Talent 1=2")
        self.assertIn("NOTE: no sheet_base.tsv", out)
        self.assertNotIn("figure ", out)

    def test_with_sheet_figures_the_table_shows_base_bonus_and_total(self):
        self.install()
        self.write_sheet_base(60, melee_crit="4.21")
        _, out = self.sheet("--talent", "Talent 2=1")  # the fixture's talent with a +2 crit effect
        self.assertNotIn("no sheet_base.tsv", out)
        crit = next(l for l in out.splitlines() if l.startswith("melee_crit"))
        self.assertIn("4.21%", crit)
        self.assertIn("+2.00%", crit)
        self.assertIn("6.21%", crit)
        self.assertIn("Talent 2 +2", crit)
        self.assertIn("9.00%", next(l for l in out.splitlines() if l.startswith("spell_crit")))  # 7.00 + 2
        self.assertIn("attack power", out)

    def test_rates_feed_extra_stats_into_the_figures(self):
        self.install()
        self.write_sheet_base(60, melee_crit="4.21")
        (self.data / "external" / "stat_rates.json").write_text(json.dumps({
            "level": 60, "agility": {"Mage": ["1/20", "1/20"]}, "intellect_spell_crit": ["1/60", "1/60"],
            "spell_classes": ["Mage"], "attack_power": {"Mage": [2, 0, -10, False]}}), encoding="utf-8")
        _, out = self.sheet("--talent", "Talent 1=2", "--adjust", "agility=20")  # Talent 1: +4% Intellect
        spell = next(l for l in out.splitlines() if l.startswith("spell_crit"))
        self.assertIn("7.00%", spell)
        self.assertIn("+0.03%", spell)  # 60 -> 62 Intellect is 2 points; 2 / 60 = 0.033, shown to two decimals
        self.assertIn("+1.00%", spell)  # 20 Agility / 20
        self.assertIn("8.03%", spell)   # 7.00 + 1.00 + 0.033
        self.assertIn("extra stats", spell)
        self.assertNotIn("no stat_rates.json", out)

    def test_without_the_rates_file_it_says_so(self):
        self.install()
        self.write_sheet_base(60, melee_crit="4.21")
        _, out = self.sheet("--talent", "Talent 1=2")
        self.assertIn("no stat_rates.json", out)

    def write_rates(self, **extra):
        data = {"level": 60, "agility": {"Mage": ["1/20", "1/20"]}, "intellect_spell_crit": ["1/60", "1/60"],
                "spell_classes": ["Mage"], "attack_power": {"Mage": [2, 0, -10, False]},
                "armor": {"per_agility": 2, "worst_dr_error": "0", "formula": "x"},
                "health": {"per_point": 10, "constants": {"Mage": 900}, "race_factors": {}},  # 900 + 10 x 60 = the fixture's 1500
                "mana": {"per_point": 15, "constants": {"Mage": 1100}, "race_factors": {}},
                "assumed": {"block_base_chance": "5", "shield_classes": ["Mage"], "strength_per_block_value": 20}}
        data.update(extra)
        (self.data / "external").mkdir(parents=True, exist_ok=True)
        (self.data / "external" / "stat_rates.json").write_text(json.dumps(data), encoding="utf-8")

    def test_armor_damage_reduction_and_gear_armor(self):
        self.install()
        (self.data / "external" / "sheet_base.tsv").write_text(
            "\n".join("\t".join(r) for r in [HEADER, ["Mage", "Human", "60", "defense", "Armor", "120", ""]]) + "\n", encoding="utf-8")
        self.write_rates()
        _, out = self.sheet("--adjust", "armor=1000")
        line = next(l for l in out.splitlines() if l.startswith("armor"))
        self.assertIn("1120", line)
        self.assertIn("damage reduction 16.92%", line)  # 1120 / (1120 + 5100 + 400)
        self.assertIn("+1000 gear", line)

    def test_extra_stamina_shows_up_in_health(self):
        self.install()
        self.write_rates()
        _, out = self.sheet("--adjust", "stamina=5")
        line = next(l for l in out.splitlines() if l.startswith("health"))
        self.assertIn("+50", line)
        self.assertIn("from extra Stamina", line)

    def test_shield_shows_block_and_its_assumptions(self):
        self.install()
        self.write_rates()
        _, out = self.sheet("--shield")
        self.assertIn("block ", out)
        self.assertIn("5.00%", next(l for l in out.splitlines() if l.startswith("block ")))
        self.assertIn("block value", out)
        self.assertIn("confirmed from the game", out)

    def test_block_against_a_higher_level_mob(self):
        self.install()
        self.write_rates()
        _, out = self.sheet("--shield", "--attacker-level", "63")
        line = next(l for l in out.splitlines() if l.startswith("block vs lvl"))
        self.assertIn("3.50%", line)
        self.assertIn("mob", line)

    def test_block_against_a_player_uses_the_smaller_step(self):
        self.install()
        self.write_rates()
        _, out = self.sheet("--shield", "--attacker-level", "63", "--vs-player")
        self.assertIn("4.40%", next(l for l in out.splitlines() if l.startswith("block vs lvl")))

    def test_attacker_level_without_a_shield_is_explained(self):
        self.install()
        self.write_rates()
        _, out = self.sheet("--attacker-level", "63")
        self.assertNotIn("block vs lvl", out)
        self.assertIn("--attacker-level needs --shield", out)

    def test_a_mistyped_adjustment_is_an_error_not_silently_ignored(self):
        self.install()
        code, out = self.sheet("--adjust", "inteligence=20")
        self.assertEqual(code, 2)
        self.assertIn("cannot adjust 'inteligence'", out)

    def test_a_level_without_figures_is_explained_not_guessed(self):
        self.install()
        self.write_sheet_base(60)
        _, out = self.sheet("--level", "30")
        self.assertIn("not estimated", out)

    def test_a_manual_adjustment(self):
        self.install()
        _, out = self.sheet("--talent", "Talent 1=1", "--adjust", "intellect=40")
        line = next(l for l in out.splitlines() if l.startswith("intellect"))
        self.assertIn("+40", line)
        self.assertIn("102", line)  # floor((60 + 40) x 1.02)

    def test_an_illegal_build_is_still_calculated_but_flagged(self):
        self.install()
        code, out = self.sheet("--talent", "Talent 3=1")  # the capstone, with nothing above it
        self.assertEqual(code, 0)
        self.assertIn("NOTE: these talents are not a legal build on their own", out)
        self.assertIn("node 3", out)

    def test_the_unmodelled_parts_are_listed(self):
        self.install()
        _, out = self.sheet("--talent", "Talent 5=2")
        self.assertIn("Part of these talents is not in the table", out)
        self.assertIn("effect 1 script", out)
        line = next(l for l in out.splitlines() if l.startswith("agility"))
        self.assertIn("61", line)  # floor(60 x 1.02)

    def test_no_mana_class_shows_a_dash(self):
        self.install()
        (self.data / "external" / "base_stats.tsv").write_text(tsv(*[
            ("Mage", "Human", *(50, "", 20, 20, 20, 20, 20), *(1500, "", 60, 60, 60, 60, 60))]), encoding="utf-8")
        _, out = self.sheet()
        self.assertTrue(any(l.startswith("mana") and l.split()[1:] == ["-"] for l in out.splitlines()))

    def test_bad_requests_exit_two_with_a_reason(self):
        self.install()
        cases = [
            (["sheet", "--race", "Human"], "needs --class and --race"),
            (["sheet", "--class", "Mage", "--race", "Human", "--talent", "Talent 1"], "must look like NAME=NUMBER"),
            (["sheet", "--class", "Mage", "--race", "Human", "--talent", "Talent 1=x"], "must look like NAME=NUMBER"),
            (["sheet", "--class", "Mage", "--race", "Human", "--adjust", "intellect"], "must look like NAME=NUMBER"),
            (["sheet", "--class", "Necromancer", "--race", "Human"], "No class 'Necromancer'"),
            (["sheet", "--class", "Mage", "--race", "Gnome"], "Mage"),
            (["sheet", "--class", "Mage", "--race", "Human", "--level", "99"], "outside the supplied range"),
            (["sheet", "--class", "Mage", "--race", "Human", "--talent", "Nothing=1"], "no talent"),
            (["sheet", "--class", "Mage", "--race", "Human", "--talent", "Talent=1"], "more than one talent"),
            (["sheet", "extra", "--class", "Mage", "--race", "Human"], ""),
        ]
        for args, expect in cases:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = main(["--build", BUILD, "--data-dir", str(self.data), *args])
            self.assertEqual(code, 2, args)
            self.assertIn(expect, out.getvalue(), args)

    def test_missing_base_stats_file(self):
        self.install(base_stats=False)
        code, out = self.sheet()
        self.assertEqual(code, 2)
        self.assertIn("python -m wowcalc.basestats", out)


if __name__ == "__main__":
    unittest.main()
