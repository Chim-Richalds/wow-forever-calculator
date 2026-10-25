"""Offline tests for wowcalc.basestats. Run: python -m unittest discover -s tests"""

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from fixtures import TempCase, base_tables, write_dataset  # noqa: E402
from basestats_fixtures import HEADER, row, tsv, warrior  # noqa: E402
from wowcalc.basestats import load_base_stats, main, map_to_game, parse_base_stats  # noqa: E402
from wowcalc.dataset import Dataset, DatasetError  # noqa: E402
from wowcalc.fetch import TABLES  # noqa: E402

class ParseTests(unittest.TestCase):
    def test_header_variants_and_values(self):
        t = parse_base_stats(tsv(row("Mage", "Human")))
        self.assertEqual(t.problems, [])
        self.assertEqual((t.levels, t.combos()), ((1, 60), [("Mage", "Human")]))
        v = t.at("Mage", "Human", 1)
        self.assertEqual((v["health"].value, v["strength"].value, v["health"].exact), (50, 20, True))
        self.assertEqual(t.at("Mage", "Human", 60)["mana"].value, 2000)

    def test_blank_mana_means_no_mana(self):
        t = parse_base_stats(tsv(warrior("Human")))
        self.assertEqual(t.problems, [])
        self.assertIsNone(t.at("Warrior", "Human", 1)["mana"].value)
        self.assertTrue(t.at("Warrior", "Human", 1)["mana"].exact)

    def test_byte_order_mark_and_blank_lines(self):
        t = parse_base_stats("\ufeff" + tsv(row("Mage", "Human")) + "\n\n")
        self.assertEqual((t.problems, len(t.rows)), ([], 1))

    def test_more_levels_are_accepted(self):
        header = ["Class", "Race"] + [f"{s}-L{lv}" for lv in (1, 30, 60) for s in
                                      ("Health", "Mana", "Strength", "Agility", "Stamina", "Intellect", "Spirit")]
        line = ["Mage", "Human"] + [str(lv + i) for lv in (1, 30, 60) for i in range(7)]
        t = parse_base_stats("\t".join(header) + "\n" + "\t".join(line) + "\n")
        self.assertEqual((t.problems, t.levels), ([], (1, 30, 60)))

    def test_problems(self):
        cases = {
            "": "the file is empty",
            tsv(header=["Race", "Class"] + HEADER[2:]): "first two columns",
            tsv(header=HEADER[:-1] + ["Luck-L60"]): "unrecognised column header 'Luck-L60'",
            tsv(header=HEADER[:-1]): "no column for spirit at level 60",
            tsv(row("Mage", "Human")[:-1]): "15 fields, expected 16",
            tsv(row("Mage", "Human", l1=(50, 100, "x", 20, 20, 20, 20))): "strength at level 1 is not a whole number: 'x'",
            tsv(row("Mage", "Human", l1=(50, 100, -4, 20, 20, 20, 20))): "strength at level 1 is negative",
            tsv(row("Mage", "Human"), row("Mage", "Human")): "duplicate row for Mage/Human",
            tsv(row("Mage", "Human", l60=(1500, 2000, 60, "", 60, 60, 60))): "agility is blank",
            tsv(row("Mage", "Human", l60=(1500, 2000, 10, 60, 60, 60, 60))): "strength goes down as level rises",
            tsv(("", "Human", *row("Mage", "Human")[2:])): "class or race is empty",
        }
        for text, expected in cases.items():
            t = parse_base_stats(text)
            self.assertTrue(any(expected in p for p in t.problems), (expected, t.problems))

    def test_mana_blank_at_only_some_levels(self):
        r = list(row("Mage", "Human"))
        r[10] = ""
        self.assertTrue(any("mana is blank at some levels" in p for p in parse_base_stats(tsv(r)).problems))

    def test_a_class_where_only_some_races_have_mana(self):
        mage_without_mana = ("Mage", "Orc") + warrior("Orc")[2:]
        t = parse_base_stats(tsv(row("Mage", "Human"), mage_without_mana))
        self.assertTrue(any("some races have mana and some do not" in p for p in t.problems))

    def test_bad_rows_are_left_out_not_fatal(self):
        t = parse_base_stats(tsv(row("Mage", "Human"), row("Mage", "Orc", l1=(50, 100, "x", 20, 20, 20, 20))))
        self.assertEqual(t.combos(), [("Mage", "Human")])


class InterpolationTests(unittest.TestCase):
    def setUp(self):
        self.t = parse_base_stats(tsv(row("Mage", "Human", l1=(100, 50, 20, 20, 20, 20, 20), l60=(1000, 2000, 80, 20, 20, 20, 20))))

    def test_exact_at_supplied_levels(self):
        self.assertEqual(self.t.at("Mage", "Human", 1)["strength"].value, 20)
        self.assertEqual(self.t.at("Mage", "Human", 60)["strength"].value, 80)
        self.assertTrue(self.t.at("Mage", "Human", 60)["strength"].exact)

    def test_linear_estimates_between_are_marked_inexact(self):
        v = self.t.at("Mage", "Human", 31)["strength"]  # 20 + 60 * 30/59 = 50.5
        self.assertEqual((v.value, v.exact), (51, False))
        self.assertEqual(self.t.at("Mage", "Human", 10)["health"].value, 237)  # 100 + 900 * 9/59 = 237.3

    def test_unchanging_stat_stays_put(self):
        self.assertEqual(self.t.at("Mage", "Human", 33)["agility"].value, 20)

    def test_rounds_half_up(self):
        t = parse_base_stats(tsv(row("Mage", "Human", l1=(10, 10, 10, 10, 10, 10, 10), l60=(11, 11, 11, 11, 11, 11, 11))))
        self.assertEqual((t.at("Mage", "Human", 30)["strength"].value, t.at("Mage", "Human", 31)["strength"].value), (10, 11))

    def test_no_mana_stays_none_when_estimated(self):
        t = parse_base_stats(tsv(warrior("Human")))
        v = t.at("Warrior", "Human", 20)["mana"]
        self.assertEqual((v.value, v.exact), (None, False))

    def test_uses_the_nearest_supplied_levels(self):
        header = ["Class", "Race"] + [f"{s}-L{lv}" for lv in (1, 30, 60) for s in
                                      ("Health", "Mana", "Strength", "Agility", "Stamina", "Intellect", "Spirit")]
        line = ["Mage", "Human"] + [str(v) for lv, v in ((1, 0), (30, 100), (60, 130)) for _ in range(7)]
        t = parse_base_stats("\t".join(header) + "\n" + "\t".join(line) + "\n")
        self.assertEqual(t.at("Mage", "Human", 15)["strength"].value, 48)   # levels 1..30: 0 + 100 * 14/29 = 48.3
        self.assertEqual(t.at("Mage", "Human", 45)["strength"].value, 115)  # levels 30..60: 100 + 30 * 15/30

    def test_out_of_range_and_unknown_combination(self):
        for level in (0, 61):
            with self.assertRaisesRegex(ValueError, "outside the supplied range"):
                self.t.at("Mage", "Human", level)
        with self.assertRaises(KeyError):
            self.t.at("Mage", "Orc", 10)


class OutlierTests(unittest.TestCase):
    def table(self, odd_l60=(1500, 2000, 60, 60, 60, 60, 60), extra=()):
        rows = [row("Mage", "Human"), row("Mage", "Orc"), row("Mage", "Troll", l60=odd_l60), *extra]
        return parse_base_stats(tsv(*rows))

    def test_flags_a_race_that_gains_much_less_than_the_others(self):
        notes = self.table(odd_l60=(1500, 2000, 60, 30, 60, 60, 60)).outliers()
        self.assertEqual(len(notes), 1)
        self.assertIn("Mage/Troll agility: gains +10 from level 1 to 60 (20 -> 30)", notes[0])
        self.assertIn("typical Mage gains +40", notes[0])
        self.assertIn("about 60 at level 60", notes[0])

    def test_small_differences_are_ignored_and_tolerance_is_adjustable(self):
        t = self.table(odd_l60=(1500, 2000, 60, 57, 60, 60, 60))
        self.assertEqual(t.outliers(), [])
        self.assertEqual(len(t.outliers(tolerance=2)), 1)

    def test_intellect_and_spirit_are_not_checked(self):
        self.assertEqual(self.table(odd_l60=(1500, 2000, 60, 60, 60, 20, 20)).outliers(), [])

    def test_a_class_with_fewer_than_three_races_is_not_judged(self):
        t = parse_base_stats(tsv(row("Mage", "Human"), row("Mage", "Orc", l60=(1500, 2000, 60, 5, 60, 60, 60))))
        self.assertEqual(t.outliers(), [])

    def test_one_odd_race_does_not_skew_the_typical_value(self):
        notes = self.table(odd_l60=(1500, 2000, 60, 5, 60, 60, 60)).outliers()
        self.assertIn("typical Mage gains +40", notes[0])

    def test_needs_at_least_two_levels(self):
        header = ["Class", "Race"] + [f"{s}-L1" for s in ("Health", "Mana", "Strength", "Agility", "Stamina", "Intellect", "Spirit")]
        t = parse_base_stats("\t".join(header) + "\nMage\tHuman\t" + "\t".join(["1"] * 7) + "\n")
        self.assertEqual(t.outliers(), [])


class GameMappingTests(TempCase):
    def game(self, classes, races, combos):
        tables = {
            "ChrClasses": (["Name_lang", "ID"], [dict(Name_lang=n, ID=str(i)) for n, i in classes]),
            "ChrRaces": (["ID", "Name_lang"], [dict(ID=str(i), Name_lang=n) for i, n in races]),
            "CharBaseInfo": (["ID", "RaceID", "ClassID", "OtherFactionRaceID"],
                             [dict(ID=str(k + 1), RaceID=str(r), ClassID=str(c), OtherFactionRaceID="0") for k, (r, c) in enumerate(combos)]),
        }
        return Dataset(write_dataset(self.root / "game", tables), "g")

    CLASSES = [("Warrior", 1), ("Mage", 8)]
    RACES = [(1, "Human"), (2, "Orc"), (33, "Human"), (95, "High Order Skyborne"), (96, "Windshaper Skyborne"), (99, "Gnoll")]

    def table(self, *pairs):
        return parse_base_stats(tsv(*[row(c, r) if c != "Warrior" else warrior(r) for c, r in pairs]))

    def test_a_perfect_match_maps_every_name(self):
        ds = self.game(self.CLASSES, self.RACES, [(1, 1), (1, 8), (2, 1)])
        mapping, problems = map_to_game(self.table(("Warrior", "Human"), ("Mage", "Human"), ("Warrior", "Orc")), ds)
        self.assertEqual(problems, [])
        self.assertEqual((mapping.class_ids, mapping.race_ids), ({"Warrior": 1, "Mage": 8}, {"Human": 1, "Orc": 2}))

    def test_a_duplicate_race_name_is_resolved_by_which_one_players_can_create(self):
        ds = self.game(self.CLASSES, self.RACES, [(1, 1), (2, 1)])  # id 33 'Human' has no combinations
        mapping, problems = map_to_game(self.table(("Warrior", "Human"), ("Warrior", "Orc")), ds)
        self.assertEqual((problems, mapping.race_ids["Human"]), ([], 1))

    def test_skyborne_variants_are_told_apart_by_class_set(self):
        ds = self.game(self.CLASSES, self.RACES, [(95, 1), (96, 8)])
        mapping, problems = map_to_game(self.table(("Warrior", "Skyborne A"), ("Mage", "Skyborne H")), ds)
        self.assertEqual((problems, mapping.race_ids), ([], {"Skyborne A": 95, "Skyborne H": 96}))

    def test_skyborne_ambiguity_and_no_match_are_reported(self):
        same = self.game(self.CLASSES, self.RACES, [(95, 1), (96, 1)])
        _, problems = map_to_game(self.table(("Warrior", "Skyborne A")), same)
        self.assertTrue(any("cannot tell which" in p for p in problems))
        none = self.game(self.CLASSES, self.RACES, [(95, 1), (96, 1)])
        _, problems = map_to_game(self.table(("Mage", "Skyborne H")), none)
        self.assertTrue(any("no Skyborne race" in p for p in problems))

    def test_missing_extra_and_unknown_entries_are_reported(self):
        ds = self.game(self.CLASSES, self.RACES, [(1, 1), (1, 8), (2, 1)])
        _, problems = map_to_game(self.table(("Warrior", "Human"), ("Warrior", "Orc"), ("Mage", "Orc")), ds)
        self.assertIn("the game allows Human/Mage but the file has no row for it", problems)
        self.assertIn("the file has Orc/Mage, which the game does not allow", problems)

    def test_unknown_class_and_race(self):
        ds = self.game(self.CLASSES, self.RACES, [(1, 1)])
        _, problems = map_to_game(parse_base_stats(tsv(row("Necromancer", "Human"), row("Mage", "Gnome"))), ds)
        self.assertTrue(any("class 'Necromancer' is not a class" in p for p in problems))
        self.assertTrue(any("race 'Gnome': not found" in p for p in problems))

    def test_a_race_the_game_does_not_let_you_create_is_not_found(self):
        ds = self.game(self.CLASSES, self.RACES, [(1, 1)])
        _, problems = map_to_game(self.table(("Warrior", "Gnoll")), ds)  # id 99 exists but has no combinations
        self.assertTrue(any("race 'Gnoll': not found" in p for p in problems))

    def test_missing_table_raises(self):
        ds = self.game(self.CLASSES, self.RACES, [(1, 1)])
        (ds.directory / "CharBaseInfo.csv").unlink()
        with self.assertRaises(DatasetError):
            map_to_game(self.table(("Warrior", "Human")), ds)


class CliTests(TempCase):
    def setUp(self):
        super().setUp()
        self.data = self.root / "data"
        (self.data / "external").mkdir(parents=True)
        self.path = self.data / "external" / "base_stats.tsv"

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--data-dir", str(self.data), *args])
        return code, out.getvalue()

    def good_file(self):  # matches the fixture game data: Human/Orc x Warrior/Mage as in base_tables' CharBaseInfo
        self.path.write_text(tsv(warrior("Human"), row("Mage", "Human"), warrior("Orc")), encoding="utf-8")

    def install_game(self, complete=True):
        tables = base_tables()
        if not complete:
            del tables["CharBaseInfo"]
        write_dataset(self.data / "raw" / "1.60.1.70450", tables)

    def test_no_file(self):
        self.assertEqual(self.run_cli()[0], 2)

    def test_a_good_file_that_matches_the_game(self):
        self.good_file(); self.install_game()
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("3 combinations, 2 classes, 2 races, levels 1, 60", out)
        self.assertIn("all 3 combinations match the game's list", out)
        self.assertIn("race IDs: Human=1, Orc=2", out)

    def test_problems_in_the_file_exit_one_and_skip_the_cross_check(self):
        self.path.write_text(tsv(row("Mage", "Human"), row("Mage", "Human")), encoding="utf-8")
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("duplicate row", out)
        self.assertNotIn("Cross-check", out)

    def test_a_mismatch_with_the_game_exits_one(self):
        self.path.write_text(tsv(warrior("Human"), row("Mage", "Human")), encoding="utf-8")  # the game also has Orc/Warrior
        self.install_game()
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("the game allows Orc/Warrior but the file has no row for it", out)

    def test_an_incomplete_build_skips_the_cross_check_and_says_how_to_fix_it(self):
        self.good_file(); self.install_game(complete=False)
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("Game cross-check skipped", out)
        self.assertIn("python -m wowcalc.update complete", out)

    def test_outliers_are_listed(self):
        self.path.write_text(tsv(row("Mage", "Human"), row("Mage", "Orc"), row("Mage", "Troll", l60=(1500, 2000, 60, 45, 60, 60, 60))), encoding="utf-8")
        code, out = self.run_cli()
        self.assertIn("1 value(s) worth re-checking", out)
        self.assertIn("Mage/Troll agility", out)

    def test_provenance_comes_from_the_sidecar_file(self):
        self.good_file()
        self.path.with_name("base_stats.meta.json").write_text(json.dumps({"source": "sixtyupgrades.com", "verification": "unverified"}))
        self.assertIn("source: sixtyupgrades.com | verification: unverified", self.run_cli()[1])
        self.assertEqual(load_base_stats(self.path).meta["source"], "sixtyupgrades.com")


# --------------------------------------------------------------------------
# The real file (skipped unless it is installed)
# --------------------------------------------------------------------------

REAL_ROOT = Path(os.environ.get("WOWCALC_DATA", REPO / "data"))
REAL_FILE = REAL_ROOT / "external" / "base_stats.tsv"
PINNED = "1.60.1.70450"
GAME = REAL_ROOT / "raw" / PINNED
GAME_COMPLETE = GAME.is_dir() and all((GAME / f"{t}.csv").is_file() for t in TABLES)


@unittest.skipUnless(REAL_FILE.is_file(), f"no base stats file at {REAL_FILE}")
class RealFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.table = load_base_stats(REAL_FILE)

    def test_the_file_is_well_formed(self):
        self.assertEqual(self.table.problems, [])
        self.assertEqual((len(self.table.rows), len(self.table.classes), self.table.levels), (56, 9, (1, 60)))

    def test_warriors_and_rogues_have_no_mana(self):
        for cls in ("Warrior", "Rogue"):
            for _, race in [c for c in self.table.combos() if c[0] == cls]:
                self.assertIsNone(self.table.at(cls, race, 60)["mana"].value)
        self.assertIsNotNone(self.table.at("Mage", "Human", 60)["mana"].value)

    @unittest.skipUnless(GAME_COMPLETE, f"a complete {PINNED} is not at {GAME}")
    def test_it_matches_the_games_own_combination_list(self):
        mapping, problems = map_to_game(self.table, Dataset(GAME, PINNED))
        self.assertEqual(problems, [])
        self.assertEqual((mapping.race_ids["Skyborne A"], mapping.race_ids["Skyborne H"]), (95, 96))


if __name__ == "__main__":
    unittest.main()
